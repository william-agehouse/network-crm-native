import AppKit
import Observation
import SwiftUI

// The people an answer names, as rows you can tick and add to a list (ask.js:222-313).
// Answer order, the first 8 and then "Show All N". Clicking a row opens the person; the
// tick beside it selects them, shift-click extends from the last ticked row (adding
// only), and the bar under the rows counts the selection, selects all, clears, and adds
// the selected people to a list, existing or new (ui.js:373-421). Ask cards never carry
// a photo (contract §5), so each row asks the engine for one by id, once, and falls back
// to initials when there is none.

/// The selection rules, kept apart from the view so the self-test can check them.
enum AskSelection {
    /// A tick on row `index`. With shift and an earlier tick, the range between them is
    /// ADDED: inverting it would let one stray shift-click un-pick twenty people
    /// (ask.js:236-249). Selection is by id, so expanding the list never renumbers it.
    static func toggle(_ sel: Set<Int>, cards: [AskCard], index i: Int, anchor: Int?, shift: Bool) -> Set<Int> {
        guard cards.indices.contains(i) else { return sel }
        var next = sel
        if shift, let a = anchor, a != i, cards.indices.contains(a) {
            for k in min(a, i)...max(a, i) { next.insert(cards[k].id) }
        } else if next.contains(cards[i].id) {
            next.remove(cards[i].id)
        } else {
            next.insert(cards[i].id)
        }
        return next
    }

    /// Row order, not click order: the list you get out reads like the answer (ask.js:260).
    static func ordered(_ cards: [AskCard], _ sel: Set<Int>) -> [Int] {
        ListsAddTo.unique(cards.filter { sel.contains($0.id) }.map(\.id))
    }

    /// The small tags on a row: the web page's Tags with max=1 (ui.js:245-261): the place,
    /// the role type, then topics only where there is room, the rest as "+N".
    static func tags(_ card: AskCard) -> [AskTag] {
        var out: [AskTag] = []
        let place = card.homeMetro.isEmpty ? card.homeCity : card.homeMetro
        if !place.isEmpty { out.append(AskTag(text: place, kind: .place)) }
        if !card.roleType.isEmpty { out.append(AskTag(text: card.roleType, kind: .role)) }
        let topics = card.topics ?? []
        let room = max(0, 1 - out.count)
        for t in topics.prefix(room) { out.append(AskTag(text: t, kind: .topic)) }
        let rest = topics.count - min(room, topics.count)
        if rest > 0 {
            out.append(AskTag(text: "+\(rest)", kind: .more, help: topics.dropFirst(room).joined(separator: ", ")))
        }
        return out
    }
}

struct AskTag: Hashable {
    enum Kind { case place, role, topic, more }
    let text: String
    let kind: Kind
    var help = ""
}

/// One answer's people and the bar for picking them off. Selection is per answer and is
/// deliberately not kept: it is a scratch gesture on the way to a list.
struct AskPeopleList: View {
    @AppEnvironment private var app
    let model: AskModel
    let turnID: Int
    let cards: [AskCard]

    @State private var selected = Set<Int>()
    /// The last row ticked: shift-click extends from here.
    @State private var anchor: Int?
    @State private var expanded = false
    @State private var choosingList = false
    static let shownFirst = 8

    var body: some View {
        let total = cards.count
        let capped = total > Self.shownFirst
        let visible = expanded ? cards : Array(cards.prefix(Self.shownFirst))
        VStack(alignment: .leading, spacing: 6) {
            VStack(spacing: 0) {
                ForEach(Array(visible.enumerated()), id: \.offset) { index, card in
                    if index > 0 { Divider().overlay(Palette.line) }
                    AskPersonRow(
                        card: card, selected: selected.contains(card.id),
                        onTick: { tick(index) })
                }
            }
            .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.panel2))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radiusSmall).strokeBorder(Palette.line))
            .clipShape(RoundedRectangle(cornerRadius: Metrics.radiusSmall))
            // A one-row answer gets no bar until you tick it (ask.js:286-288).
            if total > 1 || !selected.isEmpty {
                bar(total: total, capped: capped)
            }
        }
        .padding(.top, 2)
        .onAppear(perform: takePretick)
    }

    private func bar(total: Int, capped: Bool) -> some View {
        let n = selected.count
        return HStack(spacing: 8) {
            // The count has exactly one owner, and it is this label.
            if n > 0 {
                Text("\(n) selected")
                    .font(.system(size: Metrics.fsOverline, weight: .semibold).monospacedDigit())
                    .foregroundStyle(Palette.ink)
            }
            if total > 1 && n < total {
                Button("Select All") {
                    selected = Set(cards.map(\.id))
                    if capped { expanded = true }
                }
            }
            if n > 0 {
                Button("Clear") { clear() }
                    .accessibilityLabel("Clear selection")
                Button("Add to List…") { choosingList = true }
                    .buttonStyle(.borderedProminent)
                    .tint(Palette.accent)
                    .popover(isPresented: $choosingList, arrowEdge: .bottom) {
                        ListsAddToListView(ids: AskSelection.ordered(cards, selected)) { outcome in
                            choosingList = false
                            clear()
                            model.showToast(outcome.message)
                        }
                        .environment(app)
                    }
            }
            if capped {
                Button(expanded ? "Show Fewer" : "Show All \(total)") {
                    withAnimation(.easeOut(duration: 0.15)) { expanded.toggle() }
                }
            }
        }
        .controlSize(.small)
        .font(.system(size: Metrics.fsOverline))
    }

    private func tick(_ index: Int) {
        let shift = NSEvent.modifierFlags.contains(.shift)
        selected = AskSelection.toggle(selected, cards: cards, index: index, anchor: anchor, shift: shift)
        anchor = index
    }

    private func clear() {
        selected = []
        anchor = nil
    }

    /// `--snapshot` ticks people on the newest answer (and may open the chooser).
    private func takePretick() {
        guard let p = model.pretick, p.turnID == turnID else { return }
        model.pretick = nil
        selected = Set(p.ids)
        anchor = cards.firstIndex { p.ids.contains($0.id) }
        if p.openChooser { choosingList = true }
    }
}

/// One person an answer names: a tick, then the row that opens them.
struct AskPersonRow: View {
    @AppEnvironment private var app
    let card: AskCard
    let selected: Bool
    let onTick: () -> Void
    @State private var hovering = false

    /// roleStr, else the card's reason, else "No role recorded" (ask.js:261, never blank).
    private var meta: String {
        let role = Display.roleString(position: card.position, company: card.company)
        if !role.isEmpty { return role }
        if let r = card.reason, !r.isEmpty { return r }
        return "No role recorded"
    }

    var body: some View {
        HStack(spacing: 8) {
            Toggle(isOn: Binding(get: { selected }, set: { _ in onTick() })) { EmptyView() }
                .toggleStyle(.checkbox)
                .labelsHidden()
                .help(selected ? "Deselect \(card.name)" : "Select \(card.name). Shift-click to extend.")
                .accessibilityLabel(selected ? "Deselect \(card.name)" : "Select \(card.name)")
            Button {
                app.openPerson(card.id)
            } label: {
                HStack(spacing: 8) {
                    AskAvatar(id: card.id, name: card.name, strength: card.strength, label: card.strengthLabel, size: 24)
                    Text(verbatim: card.name)
                        .font(.system(size: 12.5, weight: .semibold))
                        .foregroundStyle(Palette.ink)
                        .lineLimit(1)
                        .layoutPriority(2)
                    Text(verbatim: meta)
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.muted)
                        .lineLimit(1)
                        .truncationMode(.tail)
                        .layoutPriority(0)
                    Spacer(minLength: 6)
                    tags
                        .layoutPriority(1)
                    if !card.strengthLabel.isEmpty {
                        HStack(spacing: 4) {
                            Circle().fill(Display.Tier(label: card.strengthLabel).color).frame(width: 6, height: 6)
                            Text(verbatim: card.strengthLabel)
                        }
                        .font(.system(size: 10.5))
                        .foregroundStyle(Palette.muted)
                        .fixedSize()
                        .frame(minWidth: 52, alignment: .leading)
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Open \(card.name)")
            .accessibilityLabel("Open \(card.name)")
        }
        .padding(.horizontal, 10)
        .frame(minHeight: 36)
        .background(selected ? Palette.accentSoft.opacity(0.55) : hovering ? Palette.hover : .clear)
        .onHover { hovering = $0 }
        .contextMenu {
            Button("Open \(card.name)") { app.openPerson(card.id) }
            Button(selected ? "Deselect" : "Select") { onTick() }
            Button("Copy Name") {
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(card.name, forType: .string)
            }
        }
    }

    private var tags: some View {
        HStack(spacing: 4) {
            ForEach(AskSelection.tags(card), id: \.self) { tag in
                Text(verbatim: tag.text)
                    .font(.system(size: 10.5))
                    .foregroundStyle(tag.kind == .topic ? Palette.accentInk : Palette.muted)
                    .lineLimit(1)
                    .padding(.horizontal, 6)
                    .padding(.vertical, 1.5)
                    .background(Capsule().fill(tag.kind == .topic ? Palette.accentSoft : .clear))
                    .overlay(Capsule().strokeBorder(tag.kind == .topic ? .clear : Palette.lineStrong))
                    .help(tag.help)
            }
        }
        .fixedSize()
    }
}

/// A round avatar: the photo when the engine has one, else initials on the name's own
/// gradient, with the closeness ring when there is a score (contract §8.4).
struct AskAvatar: View {
    @AppEnvironment private var app
    let id: Int
    let name: String
    let strength: Int
    let label: String
    let size: CGFloat

    var body: some View {
        let photos = AskPhotoStore.shared(for: app)
        let colors = Display.avatarColors(name)
        let ring = strength > 0
        let inner = ring ? size - 5 : size
        ZStack {
            if ring {
                Circle().stroke(Palette.line, lineWidth: 1.5)
                Circle()
                    .trim(from: 0, to: Display.ringFraction(strength: strength))
                    .stroke(Display.Tier(label: label).color, style: StrokeStyle(lineWidth: 1.5, lineCap: .round))
                    .rotationEffect(.degrees(-90))
            }
            Group {
                if let image = photos.image(for: id) {
                    Image(nsImage: image).resizable().scaledToFill()
                } else {
                    ZStack {
                        LinearGradient(colors: [colors.top, colors.bottom], startPoint: .topLeading, endPoint: .bottomTrailing)
                        Text(Display.initials(name))
                            .font(.system(size: inner * 0.38, weight: .semibold))
                            .foregroundStyle(colors.ink)
                    }
                }
            }
            .frame(width: inner, height: inner)
            .clipShape(Circle())
        }
        .frame(width: size, height: size)
        .help(Display.ringTooltip(strength: strength, label: label))
        .task(id: id) { await photos.load(id) }
    }
}

/// Photos by person id, fetched once each. A person without one is remembered as such, so
/// no chip asks twice.
@MainActor
@Observable
final class AskPhotoStore {
    @ObservationIgnored private static var stores: [ObjectIdentifier: AskPhotoStore] = [:]

    static func shared(for app: AppModel) -> AskPhotoStore {
        let key = ObjectIdentifier(app)
        if let s = stores[key] { return s }
        let s = AskPhotoStore(client: app.client)
        stores[key] = s
        return s
    }

    @ObservationIgnored private let client: EngineClient
    private var images: [Int: NSImage] = [:]
    @ObservationIgnored private var asked: Set<Int> = []

    private init(client: EngineClient) { self.client = client }

    func image(for id: Int) -> NSImage? { images[id] }

    func load(_ id: Int) async {
        guard id > 0, !asked.contains(id) else { return }
        asked.insert(id)
        do {
            if let data = try await client.photoData("/api/photo/\(id)"), let image = NSImage(data: data) {
                images[id] = image
            }
        } catch is CancellationError {
            // The chip scrolled away mid-request: let the next one ask again.
            asked.remove(id)
        } catch {
            // No photo to show; the initials stay.
        }
    }
}
