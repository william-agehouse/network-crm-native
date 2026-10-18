import SwiftUI

// The Settings page's own panels: the controls inside its rows (static/app/settings.js
// Acts, :287-317), what you are working on (GoalsField, :63-145), the meaning search
// (SemanticSearch, :161-237), the model (ModelPicker, :240-263), and This install.

// MARK: - The controls inside a row

struct SettingsActsView: View {
    let acts: [LedgerAct]
    let store: SettingsStore

    private static func isButton(_ kind: String) -> Bool { ["page", "open", "row"].contains(kind) }

    var body: some View {
        let blocks = acts.enumerated().filter { !Self.isButton($0.element.kind) }
        let buttons = acts.enumerated().filter { Self.isButton($0.element.kind) }
        VStack(alignment: .leading, spacing: 14) {
            ForEach(blocks, id: \.offset) { _, a in block(a) }
            if !buttons.isEmpty {
                LedgerFlow {
                    ForEach(buttons, id: \.offset) { _, a in button(a) }
                }
            }
        }
    }

    @ViewBuilder private func block(_ a: LedgerAct) -> some View {
        switch a.kind {
        case "text": LedgerTextSetting(act: a, host: store)
        case "secret":
            LedgerCredentialBox(act: a, host: store)
            if a.key == "ANTHROPIC_API_KEY" {
                Text(verbatim: LedgerWords.anthropicSpendHint)
                    .font(.system(size: Metrics.fsUI - 1))
                    .foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        case "switch": LedgerSwitch(act: a, host: store)
        case "model": SettingsModelPicker(act: a, store: store)
        case "goals": SettingsGoalsField(store: store)
        case "semantic": SettingsSemanticPanel(store: store)
        default: EmptyView()
        }
    }

    @ViewBuilder private func button(_ a: LedgerAct) -> some View {
        switch a.kind {
        case "page":
            // It names a row, so the Data page opens on it rather than at its top (NC-218).
            Button(a.label) {
                guard let app = store.app else { return }
                if a.page == "data" {
                    if !a.row.isEmpty { app.dataFocus = a.row }
                    app.section = .data
                } else if a.page == "settings" {
                    if !a.row.isEmpty { store.openRow(a.row) }
                } else if let s = AppModel.Section(rawValue: a.page) {
                    app.section = s
                }
            }
        case "open":
            LedgerOpenButton(act: a, host: store)
        case "row":
            Button(a.label) { store.openRow(a.row) }
        default:
            EmptyView()
        }
    }
}

// MARK: - The model

/// The model the paid features use. Saved on change, so it needs no button.
// from static/app/settings.js:240-263
struct SettingsModelPicker: View {
    let act: LedgerAct
    let store: SettingsStore
    @State private var busy = false
    @State private var shown: String?

    var body: some View {
        Picker("Model", selection: Binding(get: { shown ?? act.value }, set: { v in Task { await choose(v) } })) {
            ForEach(SettingsModels.choices(current: act.value), id: \.value) { o in
                Text(verbatim: o.label).tag(o.value)
            }
        }
        .pickerStyle(.menu)
        .fixedSize()
        .disabled(busy)
        .onChange(of: act.value) { shown = nil }
    }

    private func choose(_ value: String) async {
        guard value != act.value, let client = store.app?.client, !busy else { return }
        busy = true
        shown = value
        defer { busy = false }
        do {
            let d = try await LedgerSettings.save(client, key: act.key, value: value)
            store.flash(d.warning ?? "Model changed")
            await store.changed()
        } catch is CancellationError {
            shown = nil
        } catch {
            shown = nil
            store.flash(LedgerSettings.sentence(error))
        }
    }
}

// MARK: - What you are working on

/// NC-010: one box where you describe your own goals, in your own words. Return makes a new
/// line (the newline is the point); ⌘Return saves; Esc puts back what is stored. The ceiling
/// comes from the engine, and the counter appears only near it, because a character count
/// under a paragraph box reads as a limit on what you may say.
// from static/app/settings.js:63-145
struct SettingsGoalsField: View {
    @Bindable var store: SettingsStore

    var body: some View {
        if store.owner == nil {
            ProgressView().controlSize(.small)
        } else {
            VStack(alignment: .leading, spacing: 6) {
                Text("What you are working on").font(.system(size: Metrics.fsUI, weight: .semibold))
                Text("In your own words: what you are trying to do, and what you want from the people in here. Ask weighs this when it judges who is worth your attention, and briefs and draft messages read it too. Nothing can infer it from your message history — this is the half only you can tell it.")
                    .foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
                TextEditor(text: $store.goalsDraft)
                    .font(.system(size: Metrics.fsUI))
                    .scrollContentBackground(.hidden)
                    .padding(6)
                    .frame(minHeight: 96, maxHeight: 160)
                    .background(RoundedRectangle(cornerRadius: 6).fill(Palette.panel))
                    .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Palette.lineStrong, lineWidth: 1))
                    .overlay(alignment: .topLeading) {
                        if store.goalsDraft.isEmpty {
                            Text("Opening a second bakery; want introductions to flour suppliers and anyone who has taken a shop from one location to several.")
                                .foregroundStyle(Palette.faint)
                                .padding(.horizontal, 11)
                                .padding(.vertical, 6)
                                .allowsHitTesting(false)
                        }
                    }
                    .onExitCommand { if store.goalsDirty { store.revertGoals() } }
                    .accessibilityLabel("What you are working on")
                HStack(spacing: 10) {
                    if store.goalsDirty {
                        Button(store.goalsBusy ? "Saving…" : "Save") { Task { await store.saveGoals() } }
                            .buttonStyle(.borderedProminent)
                            .keyboardShortcut(.return, modifiers: .command)
                            .disabled(store.goalsBusy)
                        Text("or press ⌘↩").foregroundStyle(Palette.faint)
                    }
                    let left = store.goalsMax - store.goalsDraft.count
                    if left <= store.goalsMax / 10 {
                        Text(left < 0
                             ? "\(Display.number(-left)) characters over — saving will trim it"
                             : "\(Display.number(left)) characters left")
                            .foregroundStyle(left < 0 ? Palette.red : Palette.muted)
                    }
                }
                if store.ownerFailed {
                    LedgerFieldNote(text: "What you wrote could not be read just now. Saving here replaces it.")
                }
            }
            .frame(maxWidth: 640, alignment: .leading)
        }
    }
}

// MARK: - Meaning search

/// Optional meaning-based ranking. It needs about 2 GB of packages (PyTorch), so it is not
/// shipped; this installs it on demand into the engine's own Python and shows pip's own
/// progress. The size is stated before any click (NC-003).
// from static/app/settings.js:161-237
struct SettingsSemanticPanel: View {
    let store: SettingsStore

    var body: some View {
        if store.diagnosticsFailed && store.semantic == nil {
            Text("Semantic search status did not load.").foregroundStyle(Palette.muted)
        } else if let st = store.semantic {
            content(st)
        } else {
            ProgressView().controlSize(.small)
        }
    }

    private func content(_ st: SettingsSemanticStatus) -> some View {
        let ins = st.install
        let gb = st.sizeGB > 0 ? st.sizeGB : 2
        let running = ins.state == "running"
        let failed = ins.state == "failed"
        let help: String
        if st.available {
            help = "On. Search ranks people by meaning, not only by keyword."
        } else if running {
            help = "Installing. \(ins.message)"
        } else if ins.state == "succeeded" && ins.restartRequired {
            help = "Installed. Restart the app to turn it on."
        } else {
            help = "Off. Keyword search still works. Meaning-based ranking needs a one-time download of about \(gb) GB (PyTorch)"
                + (st.reason.isEmpty ? "" : " · \(st.reason)") + "."
        }
        return VStack(alignment: .leading, spacing: 8) {
            Text("Meaning-based ranking").font(.system(size: Metrics.fsUI, weight: .semibold))
            Text(verbatim: help).foregroundStyle(Palette.muted).fixedSize(horizontal: false, vertical: true)
            if st.available {
                Text("Installed.").foregroundStyle(Palette.muted)
            } else if running {
                HStack(spacing: 8) {
                    Button("Installing…") {}.disabled(true)
                    ProgressView().controlSize(.small)
                }
            } else {
                Button(store.semanticBusy ? "Starting…" : "\(failed ? "Retry Install" : "Install") (~\(gb) GB)") {
                    Task { await store.installSemantic() }
                }
                .buttonStyle(.borderedProminent)
                .disabled(store.semanticBusy)
            }
            if (running || failed) && !ins.tail.isEmpty {
                DisclosureGroup(failed ? "Install log (failed)" : "Install log") {
                    DataLogBox(text: ins.tail.joined(separator: "\n"), failed: failed).padding(.top, 4)
                }
            }
        }
        .frame(maxWidth: 640, alignment: .leading)
    }
}

// MARK: - Appearance

/// System, Light or Dark, stored as "system", "light" or "dark" under "appearance";
/// App.swift applies it to the window.
struct SettingsAppearance: View {
    @AppStorage("appearance") private var appearance = "system"

    var body: some View {
        LedgerStatusRow(label: "Appearance",
                        detail: "System follows your Mac: light in the day and dark at night if you set it to change.") {
            Picker("Appearance", selection: $appearance) {
                Text("System").tag("system")
                Text("Light").tag("light")
                Text("Dark").tag("dark")
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
        }
    }
}

// MARK: - Diagnostics

/// The read-only figures about this Mac (GET /api/diagnostics): what is stored and how
/// fresh the imports are. No values about any person, only counts and sizes.
struct SettingsDiagnosticsView: View {
    let store: SettingsStore
    @State private var expanded = false

    var body: some View {
        LedgerStatusRow(label: "Diagnostics", detail: "") {
            DisclosureGroup(isExpanded: $expanded) {
                if let d = store.diagnostics {
                    Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 4) {
                        row("People", people(d))
                        row("Notes", Display.number(d.notes))
                        row("Photos", Display.number(d.photos))
                        row("Saved messages", Display.number(d.archivedMessages))
                        row("People and notes file", LedgerWords.size(d.crmDbBytes))
                        row("Saved messages file", LedgerWords.size(d.archiveDbBytes))
                        row("Whole data folder", LedgerWords.size(d.diskBytes))
                        row("Oldest import", oldest(d.syncSummary))
                        row("Sources never imported", Display.number(d.syncSummary.never.count))
                    }
                    .padding(.top, 6)
                } else if store.diagnosticsFailed {
                    Text("The figures could not be read just now.").foregroundStyle(Palette.muted)
                } else {
                    ProgressView().controlSize(.small)
                }
            } label: {
                Text(expanded ? "What is stored on this Mac" : "Show what is stored on this Mac")
                    .foregroundStyle(Palette.ink)
            }
        }
    }

    private func row(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(verbatim: label).foregroundStyle(Palette.muted)
            Text(verbatim: value).monospacedDigit().foregroundStyle(Palette.ink).textSelection(.enabled)
        }
    }

    private func people(_ d: SettingsDiagnostics) -> String {
        let hidden = d.people.parts.hidden + d.people.parts.deleted
        let shown = d.people.figure?.n ?? d.people.parts.active
        return hidden > 0
            ? "\(Display.number(shown)) you can see, \(Display.number(hidden)) hidden or deleted"
            : Display.number(shown)
    }

    /// The oldest import among the sources that have imported at least once (NC-094): a
    /// summary of many sources moves with the worst one.
    private func oldest(_ s: SettingsSyncSummary) -> String {
        guard let at = s.oldest, !at.isEmpty else { return "None yet" }
        return "\(s.oldestLabel ?? "A source"), \(Display.relDate(at))"
    }
}
