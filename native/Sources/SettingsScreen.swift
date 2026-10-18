import SwiftUI

// The Settings page (static/app/settings.js, NC-114/NC-236), the place for every setting:
//
//   Needs you      only what the app cannot do without you: your name and email, parts
//                  that failed to install, a search install waiting for a restart
//   What it can do eight abilities, each an offer with what it takes
//   Appearance     System, Light or Dark (this app only)
//   This install   the version, setting up again, the data folder, the engine this app
//                  talks to, the figures about this Mac, and Remove everything (NC-314)
//
// Every word of the first two comes from GET /api/settings/ledger, and after anything you
// do here the page asks again. Anything that belongs to one source is on that source's row
// on the Data page. Reached from the sidebar, Settings… (⌘,) and ⌘K.

struct SettingsScreen: View {
    @AppEnvironment private var app
    @State private var store = SettingsStore()

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                VStack(alignment: .leading, spacing: 34) {
                    header
                    if let answer = store.answer {
                        needs(answer)
                        abilities(answer)
                    } else if let error = store.error {
                        VStack(alignment: .leading, spacing: 10) {
                            LedgerQuiet(text: error)
                            Button("Try Again") { Task { await store.load() } }
                        }
                    } else {
                        ProgressView().controlSize(.small)
                    }
                    LedgerSection(title: "Appearance") {
                        LedgerCard { SettingsAppearance() }
                    }
                    .id("appearance")
                    LedgerSection(title: "This install") {
                        SettingsInstallSection(facts: store.answer?.install, store: store)
                    }
                    .id("install")
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
            if let text = store.flashText { LedgerFlash(text: text).transition(.opacity) }
        }
        .animation(.easeOut(duration: 0.2), value: store.flashText)
        .task {
            await store.start(app: app)
            takeRequest()
        }
        .onDisappear { store.stopPolling() }
        .onChange(of: DataSettingsBus.shared.settingsRow) { takeRequest() }
        .onChange(of: DataSettingsBus.shared.announceTick) {
            if let line = DataSettingsBus.shared.announcement { store.flash(line) }
        }
    }

    /// A row the Data page asked for (#settings?row=identity), or a snapshot's "settings:<row>".
    private func takeRequest() {
        guard store.answer != nil else { return }
        if let row = DataSettingsBus.shared.settingsRow {
            DataSettingsBus.shared.settingsRow = nil
            store.openRow(row)
        }
        if let hint = app.snapshotHint, hint.hasPrefix("settings:") {
            app.snapshotHint = nil
            let row = String(hint.dropFirst("settings:".count))
            if row == "install" || row == "appearance" { store.bringIntoView(row) } else { store.openRow(row) }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text("Settings")
                .font(.system(size: Metrics.fsPage, weight: .semibold))
                .foregroundStyle(Palette.ink)
                .accessibilityAddTraits(.isHeader)
            Text("What the app can do for you, and what each one takes. Anything that belongs to one source is on that source's row on the Data page. How the app looks, and the engine it talks to, are at the end.")
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
                .frame(maxWidth: 720, alignment: .leading)
        }
    }

    // from static/app/settings.js:370-377
    private func needs(_ answer: SettingsLedgerAnswer) -> some View {
        LedgerSection(title: "Needs you", count: answer.needs.isEmpty ? nil : answer.needs.count) {
            if answer.needs.isEmpty {
                LedgerQuiet(text: "Nothing. Everything here is either on or yours to switch on.")
            } else {
                LedgerCard {
                    ForEach(Array(answer.needs.enumerated()), id: \.offset) { i, n in
                        LedgerNeedView(tone: n.tone, says: n.says) {
                            SettingsActsView(acts: n.acts, store: store)
                        }
                        if i < answer.needs.count - 1 { Divider() }
                    }
                }
            }
        }
    }

    // from static/app/settings.js:379-392
    private func abilities(_ answer: SettingsLedgerAnswer) -> some View {
        LedgerSection(
            title: "What it can do", count: answer.rows.count,
            note: "What the app can do besides importing, and what each one asks of you. Click a row to change it."
        ) {
            LedgerCard {
                LedgerColumnHeads(name: "What it does and gives you", have: "Now · what it takes")
                Divider()
                ForEach(answer.rows) { row in
                    SettingsAbilityRow(row: row, store: store)
                    if row.id != answer.rows.last?.id { Divider() }
                }
            }
            .clipShape(RoundedRectangle(cornerRadius: Metrics.radius))
        }
    }
}

// from static/app/settings.js:319-334
struct SettingsAbilityRow: View {
    let row: LedgerRow
    let store: SettingsStore

    var body: some View {
        let d = row.detail
        LedgerRowView(row: row, open: store.openKey == row.key, toggle: { store.toggle(row.key) }) {
            LedgerDetailView(hasAct: !d.acts.isEmpty, hasHow: !d.help.isEmpty, hasOther: !d.manual.isEmpty) {
                EmptyView()
            } act: {
                SettingsActsView(acts: d.acts, store: store)
            } how: {
                LedgerHelpView(text: d.help)
            } other: {
                Text(verbatim: d.manual).fixedSize(horizontal: false, vertical: true)
            } record: {
                EmptyView()
            }
        }
        .id("row-\(row.key)")
    }
}

// MARK: - This install

// from static/app/settings.js:394-415
struct SettingsInstallSection: View {
    let facts: SettingsInstallFacts?
    let store: SettingsStore
    @State private var confirmSetup = false

    var body: some View {
        LedgerCard {
            if let facts {
                LedgerStatusRow(label: "Version", detail: facts.updatesSays) {
                    Text(verbatim: "Network CRM \(facts.version)").fontWeight(.semibold).foregroundStyle(Palette.ink)
                }
                Divider()
                // NC-235: moved here from the list of abilities.
                LedgerStatusRow(
                    label: "Set up from scratch again",
                    detail: "Walks you through finding your data, importing it, and describing a few people. It takes over the app until you are done."
                ) {
                    Button(store.setupBusy ? "Starting…" : "Set Up Again…") { confirmSetup = true }
                        .disabled(store.setupBusy)
                        .confirmationDialog("Set up from scratch again?", isPresented: $confirmSetup) {
                            Button("Start Setup Now") { Task { await store.setUpAgain() } }
                            Button("Not Now", role: .cancel) {}
                        } message: {
                            Text("Nothing you have imported is removed.")
                        }
                }
                Divider()
                LedgerStatusRow(label: "Your data",
                                detail: "Everything is kept on this Mac. Back it up from the Data page.") {
                    if let folder = facts.dataFolder {
                        Button {
                            Task { await LedgerSettings.open(folder.target, host: store) }
                        } label: {
                            Label(folder.label, systemImage: "folder")
                        }
                    }
                }
                Divider()
            }
            EngineSettingsView()
            Divider()
            SettingsDiagnosticsView(store: store)
            Divider()
            HStack(spacing: 10) {
                Text("Your sources, and the passwords and addresses they need, are on the Data page.")
                    .foregroundStyle(Palette.muted)
                Button("Open the Data Page") { store.app?.section = .data }
            }
            .font(.system(size: Metrics.fsUI))
            .padding(.horizontal, 16)
            .padding(.vertical, 12)
            // NC-314: last on the page, below everything else (settings.js:508).
            Divider()
            SettingsRemoveEverything()
        }
    }
}
