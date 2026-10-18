import AppKit
import SwiftUI

// Copying a list out: plain text for a message, Markdown for a document, CSV for a sheet.
// The text and the Markdown are built here from the members already on screen, so they
// work with the network off; the CSV is the engine's, because it adds the phone numbers and
// emails this screen never loaded. Every export says in its header what it holds: how many
// of how many, and what happened to the invite status (static/app/lists.js:76-176).

enum ListsExport {
    /// The pipeline chips are clipped words; an export is prose (lists.js:89-93).
    static func prose(_ status: String) -> String {
        switch status {
        case "": "to invite"
        case "invited": "invited"
        case "replied": "replied"
        case "attending": "attending"
        case "declined": "declined"
        default: status
        }
    }

    static func proseCap(_ status: String) -> String {
        let p = prose(status)
        guard let first = p.first else { return p }
        return first.uppercased() + p.dropFirst()
    }

    /// "1 person", "3 people". Plain digits, as the web page's template string writes them.
    static func nPeople(_ n: Int) -> String { "\(n) \(n == 1 ? "person" : "people")" }

    /// One member, one line: name — role, company — place. The role falls back to the role
    /// type, as the engine's CSV does, so "investor" alone still says something (lists.js:99-102).
    static func memberLine(_ m: PersonSummary) -> String {
        let role = [m.position.isEmpty ? m.roleType : m.position, m.company].filter { !$0.isEmpty }
            .joined(separator: ", ")
        let place = m.homeMetro.isEmpty ? m.homeCity : m.homeMetro
        return [m.name, role, place].filter { !$0.isEmpty }.joined(separator: " — ")
    }

    struct Group: Equatable {
        let status: String
        let rows: [PersonSummary]
    }

    /// Members bucketed by status: the five known statuses in pipeline order, then any
    /// status the engine adds later, in the order first seen (lists.js:104-115).
    static func groups(_ members: [PersonSummary]) -> [Group] {
        var order: [String] = []
        var buckets: [String: [PersonSummary]] = [:]
        for m in members {
            let k = m.listStatus ?? ""
            if buckets[k] == nil { order.append(k) }
            buckets[k, default: []].append(m)
        }
        let known = Display.listStatuses
        let keys = known.filter { buckets[$0] != nil } + order.filter { !known.contains($0) }
        return keys.map { Group(status: $0, rows: buckets[$0] ?? []) }
    }

    struct Scoped {
        let groups: [Group]
        let shown: Int
        let total: Int
        let note: String
        let head: [String]
    }

    /// Which rows, and what the header has to say about them (lists.js:117-136).
    /// `scope` is "all" or one status.
    static func scoped(_ list: ListDetail, scope: String) -> Scoped {
        let all = list.members
        let gs = groups(all).filter { scope == "all" || $0.status == scope }
        let shown = gs.reduce(0) { $0 + $1.rows.count }
        let note: String =
            scope != "all"
            ? "\(prose(scope)) only"
            : gs.count > 1 ? "grouped by invite status" : gs.count == 1 ? "all \(prose(gs[0].status))" : ""
        let when = [list.eventDate.isEmpty ? "" : Display.fmtDay(list.eventDate), list.venue]
            .filter { !$0.isEmpty }.joined(separator: " · ")
        let count = (shown == all.count ? nPeople(shown) : "\(shown) of \(all.count) people") + (note.isEmpty ? "" : " · \(note)")
        let head = [list.name.isEmpty ? "List" : list.name, when, count].filter { !$0.isEmpty }
        return Scoped(groups: gs, shown: shown, total: all.count, note: note, head: head)
    }

    /// The plain-text roster: the header, then one block per status group, a heading on
    /// each only when there is more than one (lists.js:138-148).
    static func text(_ list: ListDetail, scope: String) -> String {
        let s = scoped(list, scope: scope)
        var blocks = [s.head.joined(separator: "\n")]
        for g in s.groups {
            let title = s.groups.count > 1 ? "\(proseCap(g.status)) (\(g.rows.count))" : ""
            blocks.append(([title] + g.rows.map(memberLine)).filter { !$0.isEmpty }.joined(separator: "\n"))
        }
        return blocks.joined(separator: "\n\n") + "\n"
    }

    /// One table cell: pipes escaped, whitespace collapsed (lists.js:150).
    static func mdCell(_ v: String) -> String {
        v.replacingOccurrences(of: "|", with: "\\|")
            .replacingOccurrences(of: #"\s+"#, with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespaces)
    }

    /// One Markdown table; status becomes a column when there is more than one group.
    /// Blank lines between the blocks, or a pasted table does not parse (lists.js:152-176).
    static func markdown(_ list: ListDetail, scope: String) -> String {
        let s = scoped(list, scope: scope)
        let withStatus = s.groups.count > 1
        let cols = ["Name", "Role", "Company", "City"] + (withStatus ? ["Status"] : [])
        var rows: [String] = []
        for g in s.groups {
            for m in g.rows {
                let cells = [m.name, m.position.isEmpty ? m.roleType : m.position, m.company,
                    m.homeMetro.isEmpty ? m.homeCity : m.homeMetro] + (withStatus ? [proseCap(g.status)] : [])
                rows.append("| " + cells.map(mdCell).joined(separator: " | ") + " |")
            }
        }
        let meta = s.head.dropFirst().joined(separator: " · ")
        let table = (["| \(cols.joined(separator: " | ")) |", "| \(cols.map { _ in "---" }.joined(separator: " | ")) |"] + rows)
            .joined(separator: "\n")
        return ["## \(s.head.first ?? "List")", meta.isEmpty ? "" : "*\(meta)*", table]
            .filter { !$0.isEmpty }.joined(separator: "\n\n") + "\n"
    }

    /// Puts text on the clipboard. False when the clipboard refused it.
    static func copy(_ text: String) -> Bool {
        let pb = NSPasteboard.general
        pb.clearContents()
        return pb.setString(text, forType: .string)
    }
}

/// The Export popover: a preview of exactly what lands on the clipboard, an optional status
/// scope, Copy as Text, Copy as Markdown and the engine's CSV (lists.js:181-284).
struct ListsExportPopover: View {
    let store: ListsStore
    let detail: ListDetail
    let close: () -> Void
    @State private var scope = "all"
    /// The text a refused copy left behind, shown selectable so ⌘C can take it.
    @State private var manual = ""

    var body: some View {
        let groups = ListsExport.groups(detail.members)
        // A status can empty while this is open: fall back to everyone (lists.js:212-214).
        let live = scope == "all" || groups.contains(where: { $0.status == scope }) ? scope : "all"
        let shown = ListsExport.scoped(detail, scope: live).shown
        let text = ListsExport.text(detail, scope: live)
        VStack(alignment: .leading, spacing: 10) {
            Text("Copy this list out")
                .font(.system(size: Metrics.fsUI, weight: .semibold))
            Text("The text comes from this Mac. The app uploads nothing.")
                .font(.system(size: Metrics.fsOverline))
                .foregroundStyle(Palette.muted)
            if groups.count > 1 {
                Picker("Who to copy", selection: Binding(get: { live }, set: { scope = $0; manual = "" })) {
                    Text("Everyone \(detail.members.count)").tag("all")
                    ForEach(groups, id: \.status) { g in
                        Text("\(Display.listStatusLabel(g.status)) \(g.rows.count)").tag(g.status)
                    }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .controlSize(.small)
            }
            Text(manual.isEmpty ? "Preview — plain text" : "Not copied. Select the text and press ⌘C.")
                .font(.system(size: Metrics.fsOverline, weight: .medium))
                .foregroundStyle(manual.isEmpty ? Palette.muted : Palette.danger)
            preview(manual.isEmpty ? text.trimmingCharacters(in: .newlines) : manual)
            HStack(spacing: 8) {
                Button("Copy as Text") { copy(text, as: "text", shown: shown) }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                Button("Copy as Markdown") {
                    copy(ListsExport.markdown(detail, scope: live), as: "Markdown", shown: shown)
                }
                Spacer(minLength: 4)
                Button {
                    close()
                    Task { await store.exportCSV(detail) }
                } label: {
                    Text(live == "all" ? "Save CSV…" : "Save CSV — all \(detail.members.count)…")
                }
                .help("Every member, with phone numbers and emails, as a file")
            }
            .controlSize(.small)
            Text(
                live == "all"
                    ? "The CSV adds one column for phone numbers and one for emails."
                    : "Text and Markdown use the \(ListsExport.prose(live)) filter above. The CSV always holds the whole list — \(ListsExport.nPeople(detail.members.count)), every status."
            )
            .font(.system(size: Metrics.fsOverline))
            .foregroundStyle(Palette.muted)
            .fixedSize(horizontal: false, vertical: true)
        }
        .padding(14)
        .frame(width: 440)
    }

    private func preview(_ body: String) -> some View {
        ScrollView {
            Text(verbatim: body)
                .font(.system(size: 11.5, design: .monospaced))
                .foregroundStyle(Palette.ink)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
        }
        .frame(height: 190)
        .background(RoundedRectangle(cornerRadius: 6).fill(Palette.panel2))
        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(manual.isEmpty ? Palette.line : Palette.danger.opacity(0.6)))
    }

    private func copy(_ body: String, as what: String, shown: Int) {
        if ListsExport.copy(body) {
            manual = ""
            store.say("Copied \(ListsExport.nPeople(shown)) as \(what)")
            close()
        } else {
            // A blind "Copied" is the failure nobody notices until they paste nothing.
            manual = body
            store.say("Could not copy. Take the text from the box.")
        }
    }
}
