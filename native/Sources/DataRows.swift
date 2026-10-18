import SwiftUI

// The Data page's rows and every control a row or a need can carry, drawn from what the
// engine sent (static/app/data.js:226-384).

// MARK: - A source row

struct DataSourceRow: View {
    let row: LedgerRow
    let store: DataStore

    var body: some View {
        LedgerRowView(row: row, open: store.openKey == row.key, showsNote: row.detail.parts == nil,
                      toggle: { store.toggle(row.key) }) {
            if let parts = row.detail.parts {
                partsDetail(parts)
            } else {
                plainDetail
            }
        }
        .id("row-\(row.key)")
    }

    /// NC-231: the three passes the app runs by itself, folded into one row: each one's
    /// state and last run under Now, and what each added under its own button.
    // from static/app/data.js:331-346
    private func partsDetail(_ parts: [LedgerPart]) -> some View {
        let recorded = row.inUse ? parts.filter { $0.inUse && !$0.provenance.isEmpty } : []
        return LedgerDetailView(hasNow: true, hasHow: !row.detail.help.isEmpty, hasRecord: !recorded.isEmpty) {
            VStack(alignment: .leading, spacing: 5) {
                ForEach(parts) { p in
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(verbatim: "•").foregroundStyle(Palette.muted)
                        Text("\(Text(verbatim: p.label + ".").fontWeight(.semibold)) \(LedgerWords.part(p))")
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
        } act: {
            EmptyView()
        } how: {
            LedgerHelpView(text: row.detail.help)
        } other: {
            EmptyView()
        } record: {
            VStack(alignment: .leading, spacing: 10) {
                ForEach(recorded) { p in DataRecordView(source: p.provenance, label: p.label, store: store) }
            }
        }
    }

    // from static/app/data.js:347-383
    private var plainDetail: some View {
        let d = row.detail
        let now = LedgerWords.now(row)
        return LedgerDetailView(
            hasNow: !now.isEmpty, hasAct: !d.acts.isEmpty, hasHow: !d.help.isEmpty, hasOther: !d.manual.isEmpty,
            hasRecord: !d.provenance.isEmpty && row.inUse
        ) {
            Text(verbatim: now).fixedSize(horizontal: false, vertical: true)
        } act: {
            DataActsView(acts: d.acts, label: row.label, keyPrefix: "row-\(row.key)", fromNeeds: false, store: store)
        } how: {
            if LedgerWords.foldsHelp(row) {
                DisclosureGroup("Show how it was set up") {
                    LedgerHelpView(text: d.help).padding(.top, 6)
                }
            } else {
                LedgerHelpView(text: d.help)
            }
        } other: {
            Text(verbatim: d.manual).fixedSize(horizontal: false, vertical: true)
        } record: {
            DataRecordView(source: d.provenance, label: row.label, store: store)
        }
    }
}

// MARK: - The controls

/// Every control a row or a need carries. Plain buttons share a wrapping line; a control
/// with a box of its own (an import, a key, a setting) gets a line to itself.
// from static/app/data.js:277-310
struct DataActsView: View {
    let acts: [LedgerAct]
    let label: String
    let keyPrefix: String
    let fromNeeds: Bool
    let store: DataStore

    private enum Segment: Identifiable {
        case block(Int, LedgerAct)
        case buttons([(Int, LedgerAct)])
        var id: Int {
            switch self {
            case .block(let i, _): i
            case .buttons(let items): items.first?.0 ?? -1
            }
        }
    }

    private static func isButton(_ kind: String) -> Bool {
        ["open", "row", "settings", "helper", "gmail"].contains(kind)
    }

    private var segments: [Segment] {
        var out: [Segment] = []
        var pending: [(Int, LedgerAct)] = []
        for (i, a) in acts.enumerated() {
            if Self.isButton(a.kind) {
                pending.append((i, a))
            } else {
                if !pending.isEmpty { out.append(.buttons(pending)); pending = [] }
                out.append(.block(i, a))
            }
        }
        if !pending.isEmpty { out.append(.buttons(pending)) }
        return out
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            ForEach(segments) { seg in
                switch seg {
                case .block(let i, let a):
                    block(a, key: "\(keyPrefix)|\(a.kind)-\(a.key)\(a.source)-\(i)")
                case .buttons(let items):
                    LedgerFlow {
                        ForEach(items, id: \.0) { _, a in button(a) }
                    }
                }
            }
        }
    }

    @ViewBuilder private func block(_ a: LedgerAct, key: String) -> some View {
        switch a.kind {
        case "import":
            DataImportAct(act: a, label: label, key: key, fromNeeds: fromNeeds, store: store)
        case "switch":
            // The calendar rows' "switch downloading on" had no control at all once (NC-215).
            LedgerSwitch(act: a, host: store)
        case "secret":
            LedgerCredentialBox(act: a, host: store)
        case "text":
            LedgerTextSetting(act: a, host: store)
        default:
            EmptyView()
        }
    }

    @ViewBuilder private func button(_ a: LedgerAct) -> some View {
        switch a.kind {
        case "open":
            LedgerOpenButton(act: a, host: store)
        case "settings":
            Button(a.label) { store.app?.openSettings(row: a.row.isEmpty ? nil : a.row) }
        case "row":
            Button(a.label) { store.openRow(a.row) }
        case "helper":
            DataSetupButton(
                title: a.state == "not_installed" ? "Set Up Automatic Copying…" : "Open Automatic Copying…",
                note: "Installing it, allowing it in System Settings and checking it works happen in their own window.",
                flow: .fullDiskAccess, store: store)
        case "gmail":
            DataSetupButton(
                title: "Set Up Gmail…",
                note: "Adding an address, Google's sign-in file and the sign-in itself happen in their own window.",
                flow: .gmail, store: store)
        default:
            EmptyView()
        }
    }
}

/// Opens one of the setup sheets another part of the app owns (SetupFlowView). The web page
/// embeds them in the row (data.js:297-300); a sheet is the Mac's way.
struct DataSetupButton: View {
    let title: String
    let note: String
    let flow: SetupFlow
    let store: DataStore
    var body: some View {
        HStack(spacing: 10) {
            Button(title) { store.app?.setupFlow = flow }
                .buttonStyle(.borderedProminent)
            Text(verbatim: note)
                .font(.system(size: Metrics.fsOverline + 1))
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

/// Import a file, or run a source that needs none.
// from static/app/data.js:226-274
struct DataImportAct: View {
    let act: LedgerAct
    let label: String
    let key: String
    let fromNeeds: Bool
    let store: DataStore

    var body: some View {
        let run = store.run(key)
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                if act.needsPath && !act.pick.isEmpty {
                    Button(act.folder ? "Choose Folder…" : "Choose File…") {
                        Task { await store.choose(act, key: key) }
                    }
                }
                if act.needsPath {
                    TextField(act.folder ? "…or paste the folder's path here" : "…or paste the file's path here",
                              text: Binding(get: { store.path(key, act: act) },
                                            set: { store.paths[key] = $0; store.setNote(key, "") }))
                        .textFieldStyle(.roundedBorder)
                        .autocorrectionDisabled()
                        .frame(minWidth: 220, maxWidth: 420)
                        .help(store.path(key, act: act))
                        .accessibilityLabel(act.folder ? "Path to the folder" : "Path to the file")
                        .onSubmit { start(dryRun: false) }
                }
                if !act.needsPath && !act.path.isEmpty {
                    if !act.pathSays.isEmpty {
                        Text(verbatim: act.pathSays).foregroundStyle(Palette.muted).help(act.path)
                    } else {
                        Text(verbatim: act.path)
                            .font(.system(size: Metrics.fsOverline + 1, design: .monospaced))
                            .foregroundStyle(Palette.muted)
                            .lineLimit(1)
                            .truncationMode(.middle)
                            .help(act.path)
                    }
                }
                if act.preview {
                    Button("Preview") { start(dryRun: true) }
                        .disabled(run.running)
                        .help("Show what would change. Change nothing.")
                }
                importButton(run)
            }
            if run.running {
                HStack(spacing: 6) {
                    ProgressView().controlSize(.small)
                    // NC-372: a run with extra fields (Read shared history) reads what is
                    // already saved rather than importing a source.
                    Text(act.form.isEmpty
                         ? "Importing. A large source can take a few minutes; the page stays usable."
                         : "Working. This can take a few minutes; the page stays usable.")
                        .font(.system(size: Metrics.fsOverline + 1))
                        .foregroundStyle(Palette.muted)
                }
            }
            if !run.note.isEmpty { LedgerFieldNote(text: run.note) }
            // In Needs you the result moves to the top of the section, because a finished
            // import removes its own need (NC-216).
            if !fromNeeds, !run.running, run.log != nil {
                DataRunResult(run: run, store: store)
            }
        }
    }

    @ViewBuilder private func importButton(_ run: DataImportRun) -> some View {
        let title = run.running ? (act.form.isEmpty ? "Importing…" : "Working…")
            : act.verb.isEmpty ? "Import" : act.verb
        if act.verb == "Import this one instead" {
            Button(title) { start(dryRun: false) }.disabled(run.running)
        } else {
            Button(title) { start(dryRun: false) }.buttonStyle(.borderedProminent).disabled(run.running)
        }
    }

    private func start(dryRun: Bool) {
        Task { await store.runImport(act, label: label, key: key, dryRun: dryRun, fromNeeds: fromNeeds) }
    }
}

/// The result of a finished run: verb-count chips ("Archived 120") with the raw output
/// tucked behind a disclosure when the log distilled cleanly; otherwise the raw log.
/// Failures always show the log open. Nothing the importer reported is dropped.
// from static/app/data.js:162-190
struct DataRunResult: View {
    let run: DataImportRun
    let store: DataStore

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // Close the loop: after an import placed new people, offer to describe them.
            if run.ok && run.added > 0 {
                Button {
                    store.app?.peopleRequest = PeopleRequest(filter: "todo")
                    store.app?.section = .people
                } label: {
                    Label("Describe \(Display.number(run.added)) new \(run.added == 1 ? "person" : "people")",
                          systemImage: "chevron.right")
                        .labelStyle(DataTrailingIcon())
                }
                .buttonStyle(.borderedProminent)
            }
            if run.ok && !run.receipts.isEmpty {
                LedgerFlow(spacing: 6) {
                    ForEach(Array(run.receipts.enumerated()), id: \.offset) { _, r in
                        HStack(spacing: 4) {
                            Text(verbatim: r.verb)
                            Text(verbatim: Display.number(r.count)).fontWeight(.semibold).monospacedDigit()
                        }
                        .font(.system(size: Metrics.fsOverline + 1))
                        .padding(.horizontal, 8)
                        .padding(.vertical, 3)
                        .background(Capsule().fill(Palette.panel))
                        .overlay(Capsule().strokeBorder(Palette.line, lineWidth: 1))
                    }
                }
                DisclosureGroup("Details") { DataLogBox(text: run.log ?? "", failed: false).padding(.top, 4) }
                    .font(.system(size: Metrics.fsOverline + 1))
            } else {
                DataLogBox(text: run.log ?? "", failed: !run.ok)
            }
        }
        .accessibilityElement(children: .contain)
    }
}

struct DataTrailingIcon: LabelStyle {
    func makeBody(configuration: Configuration) -> some View {
        HStack(spacing: 4) {
            configuration.title
            configuration.icon.font(.system(size: 9, weight: .bold))
        }
    }
}

/// An importer's own output, as it printed it.
struct DataLogBox: View {
    let text: String
    let failed: Bool
    var body: some View {
        ScrollView {
            Text(verbatim: text)
                .font(.system(size: 11, design: .monospaced))
                .foregroundStyle(failed ? Palette.red : Palette.ink)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
        }
        .frame(maxHeight: 200)
        .fixedSize(horizontal: false, vertical: true)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.panel))
        .overlay(RoundedRectangle(cornerRadius: Metrics.radiusSmall)
            .strokeBorder(failed ? Palette.red.opacity(0.5) : Palette.line, lineWidth: 1))
    }
}

// MARK: - What a source added

/// Fetched only when asked for.
// from static/app/data.js:313-320
struct DataRecordView: View {
    let source: String
    let label: String
    let store: DataStore

    var body: some View {
        if store.shownRecords.contains(source) {
            DataProvenanceView(state: store.provenance[source] ?? .loading, label: label)
        } else {
            Button("Show what \(label) added") {
                store.shownRecords.insert(source)
                Task { await store.loadProvenance(source) }
            }
        }
    }
}

/// What one source brought, with every caveat. Weight, not colour, marks a soft number: an
/// exact count is semibold ink, one that is only the current winner, the record's creator
/// or shared with another importer is regular and muted, with the reason under it.
// from static/app/data.js:119-160
struct DataProvenanceView: View {
    let state: DataProvState
    let label: String

    var body: some View {
        switch state {
        case .loading:
            ProgressView().controlSize(.small)
        case .failed, .loaded(nil):
            note("Couldn't read what \(label) brought just now. Nothing already imported is affected.")
        case .loaded(let panel?):
            if panel.recordsNothing {
                note(panel.note)
            } else {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(panel.kinds) { k in
                        VStack(alignment: .leading, spacing: 2) {
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                Text(verbatim: k.label).foregroundStyle(Palette.ink)
                                Text(verbatim: LedgerWords.provenanceCount(k))
                                    .fontWeight(k.certainty == "exact" ? .semibold : .regular)
                                    .foregroundStyle(k.certainty == "exact" ? Palette.ink : Palette.muted)
                                    .monospacedDigit()
                                if (k.count ?? 0) > 0 && k.ofTotal > 0 {
                                    Text(verbatim: "of \(Display.number(k.ofTotal))").foregroundStyle(Palette.muted)
                                }
                                if (k.count ?? 0) > 0 && !k.detail.isEmpty {
                                    Text(verbatim: k.detail).foregroundStyle(Palette.muted)
                                }
                            }
                            if !k.note.isEmpty { note(k.note) }
                        }
                    }
                    if !panel.notRecordedNote.isEmpty { note(panel.notRecordedNote) }
                }
            }
        }
    }

    private func note(_ text: String) -> some View {
        Text(verbatim: text)
            .font(.system(size: Metrics.fsOverline + 1))
            .foregroundStyle(Palette.muted)
            .fixedSize(horizontal: false, vertical: true)
    }
}
