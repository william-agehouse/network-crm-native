import AppKit
import SwiftUI

// NC-344. SwiftUI's Table is an NSOutlineView with "automatic row heights" switched on:
// each time rows are inserted, AppKit builds a full row view for EVERY inserted row just to
// measure how tall it is, and each of those registers itself with the window. For a few
// thousand people that is thousands of throwaway rows. Sampled during such a freeze after a
// re-sort, the main thread was inside
//     -[NSTableRowData _doAutomaticRowHeightsForInsertedAndVisibleRows]
// building row views, and measuring those rows runs SwiftUI updates from inside the table's
// own update, which is the "reentrant operation in its NSTableView delegate" warning.
//
// Every People row is the same height, so nothing needs measuring. This invisible view sits
// behind the table (same frame), finds the table that covers it, and gives it one fixed row
// height, which makes inserting thousands of rows as cheap as showing the ones on screen.
//
// NC-360. It also keeps the open columns inside the table's width. Opening a person hides
// three columns and narrows the table, but the three left keep the widths they grew to
// across the whole window (Name 399, Closeness 160, Last contact 150 in a window 1,000
// points wide, against 589 points of table), so the table could slide sideways and hide
// the names. Whenever the table or its visible area changes width, a table wider than
// what shows is fitted back with the table's own sizeToFit, which keeps every minimum.

struct PeopleTableTuner: NSViewRepresentable {
    /// The whole row (separator gap included).
    var rowHeight: CGFloat

    func makeNSView(context: Context) -> PeopleTableTunerView {
        let v = PeopleTableTunerView()
        v.rowHeight = rowHeight
        return v
    }

    func updateNSView(_ view: PeopleTableTunerView, context: Context) {
        view.rowHeight = rowHeight
        view.apply()
    }
}

final class PeopleTableTunerView: NSView {
    var rowHeight: CGFloat = Metrics.rowHeight
    private weak var table: NSTableView?
    private var watches: [NSKeyValueObservation] = []
    private var frameWatches: [NSObjectProtocol] = []
    private var retries = 0
    private var fitQueued = false
    /// Fits per visible width and column count: if SwiftUI kept putting its own widths back,
    /// this stops after a few rounds instead of trading widths with it for ever.
    private var fits: [String: Int] = [:]

    /// Off the window, nothing to watch: the observers go, and come back with the window.
    override func viewWillMove(toWindow newWindow: NSWindow?) {
        super.viewWillMove(toWindow: newWindow)
        if newWindow == nil {
            for w in frameWatches { NotificationCenter.default.removeObserver(w) }
            frameWatches = []
        }
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        apply()
    }

    override func layout() {
        super.layout()
        if liveTable == nil { apply() }
    }

    /// The table, unless it has left this window. Opening or closing a person swaps the
    /// table for a new one, and the old one can still be found for a moment: a view that
    /// latched onto it lost it a breath later and never looked again (NC-360).
    private var liveTable: NSTableView? {
        guard let table, table.window != nil, table.window === window else { return nil }
        return table
    }

    private func forgetTable() {
        watches = []
        for w in frameWatches { NotificationCenter.default.removeObserver(w) }
        frameWatches = []
        fits = [:]
        table = nil
    }

    /// Finds the table and switches its automatic heights off; if SwiftUI ever turns them
    /// back on, or changes the height, the observations put them right again.
    func apply() {
        guard window != nil else { return }
        if table != nil, liveTable == nil {
            forgetTable()
            retries = 0
        }
        if table == nil, let found = findTable() {
            table = found
            // SwiftUI sets both again from time to time (a short table gets 24-point rows).
            watches = [
                found.observe(\.usesAutomaticRowHeights, options: [.new]) { [weak self] _, _ in
                    MainActor.assumeIsolated { self?.fix() }
                },
                found.observe(\.rowHeight, options: [.new]) { [weak self] _, _ in
                    MainActor.assumeIsolated { self?.fix() }
                },
            ]
        }
        if let table, frameWatches.isEmpty {
            // The table's width changes when columns hide or show; the visible area's when
            // the person's page opens, closes or is dragged wider.
            let views: [NSView] = [table] + (table.enclosingScrollView.map { [$0.contentView] } ?? [])
            frameWatches = views.map { v in
                v.postsFrameChangedNotifications = true
                return NotificationCenter.default.addObserver(
                    forName: NSView.frameDidChangeNotification, object: v, queue: .main
                ) { [weak self] _ in
                    MainActor.assumeIsolated { self?.queueFit() }
                }
            }
        }
        if table == nil {
            // The table may be made a while after this view: a new one every time a person
            // opens or closes (PeopleScreen's .id), laid out only after the page beside it.
            // Twenty immediate retries were spent before it existed, so that table kept
            // AppKit's measured rows and its too-wide columns (NC-360). Look every 50 ms
            // for up to 3 s instead.
            guard retries < 60 else { return }
            retries += 1
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in self?.apply() }
            return
        }
        fix()
        queueFit()
    }

    /// After the current layout pass, never inside it: resizing columns from within the
    /// table's own update is what AppKit warns about as reentrant.
    private func queueFit() {
        guard !fitQueued else { return }
        fitQueued = true
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.fitQueued = false
            self.fitColumns()
        }
    }

    private func fitColumns() {
        guard let table = liveTable, let clip = table.enclosingScrollView?.contentView else {
            apply()
            return
        }
        // On macOS 26 the table runs on under the see-through sidebar: the strip under it is
        // the clip view's left inset, and only the rest shows.
        // A column edge being dragged: that width is chosen on purpose, so the table may run
        // wider until the layout next changes.
        if let header = table.headerView, header.resizedColumn >= 0 { return }
        let shows = clip.bounds.width - clip.contentInsets.left - clip.contentInsets.right
        let over = table.frame.width - shows
        guard shows > 1, over > 0.5 else { return }
        let open = table.tableColumns.filter { !$0.isHidden }
        let slack = open.reduce(0) { $0 + max(0, $1.width - $1.minWidth) }
        guard slack > 0.5 else { return }  // every column is at its minimum already
        let key = "\(Int(shows)) \(open.count)"
        guard fits[key, default: 0] < 3 else { return }
        fits[key, default: 0] += 1
        // Each column gives up the same share of what it has above its minimum.
        let share = min(1, over / slack)
        for column in open {
            column.width = (column.width - max(0, column.width - column.minWidth) * share).rounded(.down)
        }
        let rest = -clip.contentInsets.left
        if clip.bounds.origin.x != rest {
            clip.scroll(to: NSPoint(x: rest, y: clip.bounds.origin.y))
            table.enclosingScrollView?.reflectScrolledClipView(clip)
        }
    }

    private func fix() {
        guard let table else { return }
        let height = max(1, rowHeight - table.intercellSpacing.height)
        if table.usesAutomaticRowHeights { table.usesAutomaticRowHeights = false }
        if abs(table.rowHeight - height) > 0.5 { table.rowHeight = height }
    }

    /// The table whose scroll view covers this view (a background has the table's frame).
    /// Matching by place, not by the first table found, keeps the sidebar's list out of it.
    private func findTable() -> NSTableView? {
        guard let root = window?.contentView?.superview ?? window?.contentView else { return nil }
        let mine = convert(bounds, to: nil)
        guard mine.width > 1, mine.height > 1 else { return nil }
        var best: (NSTableView, CGFloat, CGFloat)?
        Self.tables(in: root) { t in
            let box = (t.enclosingScrollView ?? t).convert((t.enclosingScrollView ?? t).bounds, to: nil)
            let overlap = box.intersection(mine)
            guard !overlap.isNull else { return }
            // How much of this view the table covers. Not overlap over the larger of the two:
            // the table's scroll view also runs under the sidebar, and with a person open that
            // strip made the table score 0.59 and go unfound (NC-360). The sidebar's own list
            // covers none of this view.
            let score = (overlap.width * overlap.height) / max(1, mine.width * mine.height)
            guard score > 0.9 else { return }
            // While a table is being swapped both cover this view; the new one is laid out
            // for this view's width, the old one still for the width it had.
            let area = box.width * box.height
            if best == nil || score > best!.1 + 0.01 || (abs(score - best!.1) <= 0.01 && area < best!.2) {
                best = (t, score, area)
            }
        }
        return best?.0
    }

    private static func tables(in view: NSView, _ visit: (NSTableView) -> Void) {
        if let t = view as? NSTableView {
            visit(t)
            return
        }
        for sub in view.subviews { tables(in: sub, visit) }
    }

    override func hitTest(_ point: NSPoint) -> NSView? { nil }
}
