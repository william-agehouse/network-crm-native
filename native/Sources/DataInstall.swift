import SwiftUI

// The Data page's "This install": read-only figures about the whole install, the possible
// duplicates, and the export (static/app/data.js:419-519).

struct DataInstallSection: View {
    let install: LedgerInstall
    let store: DataStore

    var body: some View {
        LedgerCard {
            peopleRow
            Divider()
            // NC-152, moved from the old page's introduction with the switch-over.
            Text("\(Text("Hiding someone is a filter, not a delete.").fontWeight(.semibold).foregroundStyle(Palette.ink)) They leave every view, search and AI answer, but they stay in the files in the data folder, and in the search index, which is built over everyone. Anything that copies that folder copies them too. To remove someone for good, delete them.")
                .foregroundStyle(Palette.muted)
                .font(.system(size: Metrics.fsUI))
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: 720, alignment: .leading)
                .padding(.horizontal, 16)
                .padding(.vertical, 12)
            Divider()
            LedgerStatusRow(label: "Messages", detail: byChannel) {
                LedgerFigureText(fig: install.messages.figure, words: "saved on this Mac")
            }
            Divider()
            LedgerStatusRow(label: "On disk", detail: diskParts) {
                Text(verbatim: LedgerWords.size(install.disk.bytes)).fontWeight(.semibold).foregroundStyle(Palette.ink)
            }
            if !install.empty {
                Divider()
                DataDuplicatesView(count: install.duplicates, store: store).id("dups")
            }
            Divider()
            exportRow.id("export")
            Divider()
            HStack(spacing: 10) {
                Text("What the app does for you, and your name and email, are in Settings.")
                    .foregroundStyle(Palette.muted)
                Button("Open Settings") { store.app?.section = .settings }
            }
            .font(.system(size: Metrics.fsUI))
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
        }
    }

    // from static/app/data.js:473-487
    private var peopleRow: some View {
        let people = install.people
        let left = people.parts.hidden + people.parts.deleted
        let detail = [
            left > 0 ? "\(Display.number(left)) more you hid or deleted" : "",
            // The count stopped adding up: say so, in the engine's words (figures.ledger_says).
            people.unexplained != 0 && !people.says.isEmpty ? LedgerWords.capitalised(people.says) : "",
        ].filter { !$0.isEmpty }.joined(separator: ". ")
        return LedgerStatusRow(label: "People", detail: detail) {
            LedgerFigureText(fig: people.figure, words: "you can see and use", whole: false)
        } extra: {
            Button("Paste a List of People…") { store.app?.setupFlow = .paste }
                .padding(.top, 2)
                .help("Add several people at once from text you copied: a list of names, an email signature, a spreadsheet column.")
        }
    }

    private var byChannel: String {
        install.messages.byChannel.filter { $0.value > 0 }
            .sorted { $0.value != $1.value ? $0.value > $1.value : $0.key < $1.key }
            .map { "\(Display.channelLabel($0.key)) \(Display.number($0.value))" }
            .joined(separator: " · ")
    }

    /// What the files in the data folder are, largest first, in plain names (at most four).
    // from static/app/data.js:56-57, :477
    private var diskParts: String {
        install.disk.named.filter { $0.bytes >= 1_000_000 }.prefix(4)
            .map { "\($0.name) \(LedgerWords.size($0.bytes))" }.joined(separator: " · ")
    }

    @ViewBuilder private var exportRow: some View {
        if install.empty {
            LedgerStatusRow(label: "Export a backup") {
                Text("Nothing to back up yet. Once people are here, this saves them and your notes as a file.")
                    .foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        } else {
            LedgerStatusRow(label: "Export a backup",
                            detail: "Your people and your notes. Messages are not included and stay on this Mac.") {
                HStack(spacing: 8) {
                    ForEach(DataExportFormat.allCases) { f in
                        Button(store.exporting == f.rawValue ? "Exporting…" : f.label + "…") {
                            Task { await store.export(f) }
                        }
                        .disabled(store.exporting != nil)
                        .help("Saves a .\(f.rawValue) file where you choose.")
                    }
                }
            }
        }
    }
}

// MARK: - Possible duplicates

/// NC-228: the duplicates finder, as one line of This install.
// from static/app/data.js:421-470
struct DataDuplicatesView: View {
    let count: Int?
    let store: DataStore

    var body: some View {
        LedgerStatusRow(
            label: "Possible duplicates",
            detail: "Found by identical names, a shared phone number or a shared email address. Merging cannot be undone."
        ) {
            Text(verbatim: LedgerWords.duplicates(count)).fontWeight(.semibold).foregroundStyle(Palette.ink)
        } extra: {
            VStack(alignment: .leading, spacing: 12) {
                HStack(spacing: 8) {
                    if (count ?? 0) > 0 {
                        Button(store.dupsOpen ? "Hide Them" : "Review Them") {
                            store.dupsOpen.toggle()
                            if store.dupsOpen && store.groups == nil { Task { await store.loadDuplicates() } }
                        }
                        .accessibilityValue(store.dupsOpen ? "shown" : "hidden")
                    }
                    Button("Merge Two People by Name…") { store.app?.mergeRequest = MergeRequest() }
                }
                .padding(.top, 4)
                if store.dupsOpen { list }
            }
        }
    }

    @ViewBuilder private var list: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let groups = store.groups {
                if groups.isEmpty {
                    LedgerQuiet(text: "None left.")
                }
                ForEach(Array(groups.enumerated()), id: \.offset) { _, g in
                    DataDupGroup(group: g, store: store)
                        .id(g.map(\.id).map(String.init).joined(separator: "-"))
                }
                LedgerQuiet(text: "It does not match “Bob” to “Robert”. Use Merge Two People by Name for those.")
            } else {
                ProgressView().controlSize(.small)
            }
        }
    }
}

/// One group of records that may be one person: choose the one to keep, then fold the
/// others into it. The default is the most filled record, so a bare row cannot silently win
/// over the enriched one and lose its facts.
// from static/app/data.js:192-221
struct DataDupGroup: View {
    let group: [DataDupMember]
    let store: DataStore
    @State private var keep: Int?
    @State private var confirming = false

    private var kept: DataDupMember? { group.first { $0.id == chosen } }
    private var chosen: Int? { keep ?? LedgerWords.bestToKeep(group) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            ForEach(group) { m in
                Button { keep = m.id } label: { option(m) }
                    .buttonStyle(.plain)
                    .accessibilityAddTraits(chosen == m.id ? [.isSelected] : [])
                    .accessibilityHint("Keeps this record")
                if m.id != group.last?.id { Divider().padding(.leading, 40) }
            }
            Divider()
            HStack(spacing: 8) {
                Button("Merge into Selected…") { confirming = true }
                    .buttonStyle(.borderedProminent)
                    .disabled(store.merging || kept == nil)
                if group.count == 2, let keepID = chosen, let other = group.first(where: { $0.id != keepID }) {
                    Button("Compare Side by Side…") {
                        store.app?.mergeRequest = MergeRequest(firstID: keepID, secondID: other.id)
                    }
                }
                if store.merging { ProgressView().controlSize(.small) }
            }
            .padding(10)
        }
        .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.panel))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radiusSmall).strokeBorder(Palette.line, lineWidth: 1))
        .confirmationDialog(
            "Fold \(LedgerWords.plural(group.count - 1, "record")) into “\(kept?.name ?? "")”?",
            isPresented: $confirming
        ) {
            Button("Confirm Merge", role: .destructive) {
                if let id = chosen { Task { await store.merge(group, keep: id) } }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The facts move across. The extra records are deleted. You cannot undo this.")
        }
    }

    private func option(_ m: DataDupMember) -> some View {
        HStack(spacing: 10) {
            Image(systemName: chosen == m.id ? "largecircle.fill.circle" : "circle")
                .foregroundStyle(chosen == m.id ? Palette.accent : Palette.faint)
                .font(.system(size: 13))
            MergeAvatar(name: m.name, photo: "", size: 26)
            VStack(alignment: .leading, spacing: 1) {
                Text(verbatim: m.name).font(.system(size: Metrics.fsUI, weight: .medium)).foregroundStyle(Palette.ink)
                let meta = LedgerWords.dupMeta(m)
                if !meta.isEmpty {
                    Text(verbatim: meta)
                        .font(.system(size: Metrics.fsOverline + 1))
                        .foregroundStyle(Palette.muted)
                        .lineLimit(2)
                }
            }
            Spacer(minLength: 0)
            if chosen == m.id {
                Text("Kept")
                    .font(.system(size: Metrics.fsOverline, weight: .medium))
                    .foregroundStyle(Palette.accentInk)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .contentShape(Rectangle())
    }
}
