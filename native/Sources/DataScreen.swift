import SwiftUI

// The Data page (static/app/data.js, NC-088/NC-236). Three sections:
//
//   Needs you      only what the app cannot do without you, cheapest to fix first
//   Your sources   every source, always: the ones you use first, then the rest cheapest
//                  to switch on first (an unused source is the app's own menu of what is
//                  possible), and the app's own work last
//   This install   read-only figures about the whole install, duplicates and the export
//
// Every word and number comes from GET /api/data/ledger. The page decides nothing: it draws
// the answer, and after anything you do on it, it asks again. app.dataFocus opens one part
// ("duplicates", "export", "merge" or a row's key) and is then cleared.

struct DataScreen: View {
    @AppEnvironment private var app

    var body: some View {
        DataPage(store: DataStore.store(for: app))
    }
}

/// The page itself, drawn from the window's one DataStore.
struct DataPage: View {
    @AppEnvironment private var app
    let store: DataStore

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 34) {
                    header
                    if let answer = store.answer {
                        DataNeedsSection(answer: answer, store: store)
                        sources(answer)
                        LedgerSection(title: "This install") {
                            DataInstallSection(install: answer.install, store: store)
                        }
                    } else if let error = store.error {
                        VStack(alignment: .leading, spacing: 10) {
                            LedgerQuiet(text: error)
                            Button("Try Again") { Task { await store.load(quickFirst: true) } }
                        }
                    } else {
                        ProgressView().controlSize(.small).padding(.top, 20)
                    }
                }
                .padding(.horizontal, 28)
                .padding(.top, 22)
                .padding(.bottom, 48)
                .frame(maxWidth: 1180, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .onChange(of: store.scrollTick) {
                guard let target = store.scrollTarget else { return }
                withAnimation(.easeInOut(duration: 0.25)) { proxy.scrollTo(target, anchor: .top) }
            }
        }
        .background(Palette.panel)
        .overlay(alignment: .bottom) {
            if let text = store.flashText {
                LedgerFlash(text: text).transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.2), value: store.flashText)
        .task {
            // Before the load: focus waits for the first quick answer, not the slow check
            // of this Mac's folders that follows it.
            takeFocus()
            await store.start(app: app)
        }
        .onChange(of: app.dataFocus) { takeFocus() }
        // The setup sheets and the merge sheet change what this page shows: ask again when
        // one closes.
        .onChange(of: app.setupFlow) { old, new in
            if old != nil && new == nil { Task { await store.changed() } }
        }
        .onChange(of: app.mergeRequest) { old, new in
            if old != nil && new == nil {
                Task {
                    if store.groups != nil { await store.loadDuplicates() }
                    await store.changed()
                }
            }
        }
        .onChange(of: DataSettingsBus.shared.announceTick) {
            if let line = DataSettingsBus.shared.announcement { store.flash(line) }
        }
    }

    private func takeFocus() {
        guard let what = app.dataFocus else { return }
        app.dataFocus = nil
        store.focus(what)
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Data")
                .font(.system(size: Metrics.fsPage, weight: .semibold))
                .foregroundStyle(Palette.ink)
                .accessibilityAddTraits(.isHeader)
            // NC-215: each row says whether it reaches the internet.
            Text("Everything this app knows about people comes from the sources below, and it is kept only on this Mac. Most imports never touch the internet. The LinkedIn messages, Gmail, Google contact photos and calendar link rows fetch from those services, LinkedIn profile facts come from your other Mac, and Facts written by AI sends short pieces of your messages to Anthropic, the company that makes Claude. Read shared history, on the LinkedIn profile facts row, sends the profiles your other Mac saved to Anthropic too. Each row says which it is.")
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: 720, alignment: .leading)
        }
    }

    private func sources(_ answer: LedgerAnswer) -> some View {
        LedgerSection(
            title: "Your sources", count: answer.rows.count,
            note: answer.inUse > 0
                ? "The ones you use first, then the rest, cheapest to switch on first. Click a row to see what it holds and how to switch it on."
                : "Cheapest to switch on first. Click a row to see what it holds and how to switch it on."
        ) {
            LedgerCard {
                LedgerColumnHeads(name: "Source and what it gives you", have: "You have now · what it takes, or its last import")
                Divider()
                ForEach(answer.rows) { row in
                    DataSourceRow(row: row, store: store)
                    if row.id != answer.rows.last?.id { Divider() }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: Metrics.radius))
        }
        .id("sources")
    }
}

// MARK: - Needs you

struct DataNeedsSection: View {
    let answer: LedgerAnswer
    let store: DataStore

    var body: some View {
        LedgerSection(title: "Needs you", count: answer.needs.isEmpty ? nil : answer.needs.count) {
            VStack(alignment: .leading, spacing: 12) {
                if let ran = store.ran { ranBox(ran) }
                if answer.needs.isEmpty {
                    allClear
                } else {
                    LedgerCard {
                        ForEach(Array(answer.needs.enumerated()), id: \.offset) { i, n in
                            LedgerNeedView(tone: n.tone, says: n.says) {
                                DataActsView(acts: n.acts, label: label(n), keyPrefix: "need-\(n.row)|\(n.source)|\(n.tone)",
                                             fromNeeds: true, store: store)
                            }
                            if i < answer.needs.count - 1 { Divider() }
                        }
                    }
                    scanLine
                }
            }
        }
        .id("needs")
    }

    private func label(_ n: LedgerNeed) -> String {
        answer.rows.first { $0.key == n.row }?.label ?? n.row
    }

    /// An import started from Needs you keeps its result here after its need goes.
    private func ranBox(_ ran: DataRan) -> some View {
        LedgerCard(padding: 12) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(verbatim: ran.label).font(.system(size: Metrics.fsUI, weight: .semibold))
                    Spacer()
                    Button("Dismiss") { store.ran = nil }.controlSize(.small)
                }
                DataRunResult(run: ran.run, store: store)
            }
        }
    }

    /// "I could not look" is a different answer from "nothing found" (NC-033, NC-034). A
    /// check that never answers is almost always macOS waiting on a folder permission.
    // from static/app/data.js:524-532
    @ViewBuilder private var scanFailed: some View {
        let text = store.scanFailed == "timeout"
            ? "Couldn't check this Mac for new files. macOS is most likely waiting for you to allow access to your Desktop and Downloads folders: look for a permission dialog, or allow it in System Settings, Privacy & Security, Files and Folders. Everything already imported is unaffected."
            : "Couldn't check this Mac for new files just now. Everything already imported is unaffected."
        LedgerQuiet(text: text, color: Display.Staleness.warm.color)
    }

    /// Under the needs: the file check's state, and the explicit way to run it.
    @ViewBuilder private var scanLine: some View {
        if !store.scanFailed.isEmpty {
            scanFailed
            checkButton
        } else if store.scanning {
            scanningLine(long: false)
        } else if !answer.scanned {
            notLookedYet
        }
    }

    private func scanningLine(long: Bool) -> some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            LedgerQuiet(text: long
                ? "Checking this Mac for files to import… If macOS asks whether Network CRM may read a folder, your answer only affects that one source."
                : "Checking this Mac for files to import…", color: Palette.faint)
        }
    }

    /// The scan never runs by itself against a development engine, and may not have run
    /// yet: say so instead of claiming nothing was found.
    private var notLookedYet: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            // The button sits right after the sentence, not at the far end of a wide line.
            Text("This page has not looked on this Mac for new files to import yet.")
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.muted)
                .fixedSize()
            checkButton
            Spacer(minLength: 0)
        }
    }

    private var checkButton: some View {
        Button("Check This Mac for Files") { Task { await store.checkAgain() } }
            .controlSize(.small)
            .disabled(store.scanning)
            .help("Looks in Desktop, Downloads and the apps' own folders for files to import. macOS may ask you to allow it.")
    }

    /// The quiet line under Needs you: what the page knows, and only once it knows it.
    // from static/app/data.js:536-558
    @ViewBuilder private var allClear: some View {
        if !store.scanFailed.isEmpty {
            scanFailed
            checkButton
        } else if store.scanning && !answer.scanned {
            scanningLine(long: true)
        } else {
            VStack(alignment: .leading, spacing: 8) {
                if answer.inUse > 0 {
                    LedgerQuiet(text: inUseLine)
                } else if let next = answer.next {
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text("Nothing is switched on yet. The cheapest place to start is \(Text(verbatim: next.label).fontWeight(.semibold).foregroundStyle(Palette.ink)): \(next.takes)")
                            .font(.system(size: Metrics.fsUI))
                            .foregroundStyle(Palette.muted)
                            .fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: 640, alignment: .leading)
                        Button("Open \(next.label)") { store.openRow(next.row) }.controlSize(.small)
                    }
                } else {
                    LedgerQuiet(text: "Nothing.")
                }
                if !answer.scanned && !store.scanning { notLookedYet }
            }
        }
    }

    private var inUseLine: String {
        var s = "Nothing. " + (answer.inUse == 1
            ? "The one source you use is working."
            : "The \(Display.number(answer.inUse)) sources you use are all working.")
        if let n = answer.newest, n.seconds != nil {
            s += " The newest import was \(n.label), \(LedgerWords.ago(n.seconds))."
        }
        return s
    }
}
