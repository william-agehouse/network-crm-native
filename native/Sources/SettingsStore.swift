import Foundation
import Observation

// The Settings page's state and every engine call it makes (static/app/settings.js). Every
// word on the page comes from one answer, GET /api/settings/ledger (crm/api_abilities.py),
// and after anything you do here the page asks again.
//
//   GET  /api/settings/ledger                 the eight abilities, what needs you, this install
//   POST /api/settings {key, value}           every box and switch (ledger.js:242-244)
//   GET  /api/owner                           what you are working on (settings.js:69)
//   POST /api/owner/goals {goals}             settings.js:86-87
//   GET  /api/diagnostics                     the figures about this Mac, and the meaning
//                                             search's install state (settings.js:167-170)
//   POST /api/semantic/install {}             downloads about 2 GB (settings.js:192). Wired;
//                                             never pressed by the self-tests.
//   POST /api/onboarding {done: false}        Set up from scratch again (settings.js:273)
//   POST /api/import/open {source: "data"}    Show the data folder (ui.js:29-32)

@MainActor
@Observable
final class SettingsStore: LedgerHost {
    private(set) var answer: SettingsLedgerAnswer?
    private(set) var error: String?
    var openKey = ""
    private(set) var scrollTarget: String?
    private(set) var scrollTick = 0
    private(set) var flashText: String?

    // What you are working on.
    private(set) var owner: SettingsOwner?
    private(set) var ownerFailed = false
    var goalsDraft = ""
    private(set) var goalsBusy = false

    // The figures about this Mac, and the meaning search.
    private(set) var diagnostics: SettingsDiagnostics?
    private(set) var diagnosticsFailed = false
    private(set) var semanticBusy = false

    private(set) var setupBusy = false

    @ObservationIgnored weak var app: AppModel?
    @ObservationIgnored private var asked = 0
    @ObservationIgnored private var flashTask: Task<Void, Never>?
    @ObservationIgnored private var pollTask: Task<Void, Never>?
    @ObservationIgnored private var wasInstalling = false

    private var client: EngineClient? { app?.client }

    // MARK: Loading

    func start(app: AppModel) async {
        self.app = app
        async let ledger: Void = load()
        async let who: Void = loadOwner()
        async let diag: Void = loadDiagnostics()
        _ = await (ledger, who, diag)
        app.markSettled()
    }

    // from static/app/settings.js:342-351
    func load() async {
        guard let client else { return }
        asked += 1
        let mine = asked
        do {
            let d = try await client.get("/api/settings/ledger", as: SettingsLedgerAnswer.self)
            guard mine == asked else { return }
            answer = d
            error = nil
        } catch is CancellationError {
        } catch {
            guard mine == asked else { return }
            if answer == nil { self.error = LedgerSettings.sentence(error) } else { flash(LedgerSettings.sentence(error)) }
        }
    }

    func changed() async { await load() }

    func openRow(_ key: String) {
        openKey = key
        bringIntoView("row-\(key)")
    }

    func bringIntoView(_ id: String) {
        scrollTarget = id
        scrollTick += 1
    }

    func toggle(_ key: String) { openKey = openKey == key ? "" : key }

    func flash(_ text: String) {
        flashText = text
        flashTask?.cancel()
        flashTask = Task { [weak self] in
            try? await Task.sleep(for: .seconds(2.6))
            guard !Task.isCancelled else { return }
            self?.flashText = nil
        }
    }

    // MARK: What you are working on (NC-010)

    /// A failure here must not take the page down with it: the name, email and key beside
    /// it are what a broken install needs to reach first.
    // from static/app/settings.js:68-74
    func loadOwner() async {
        guard let client else { return }
        do {
            let o = try await client.get("/api/owner", as: SettingsOwner.self)
            owner = o
            goalsDraft = o.goals
            ownerFailed = false
        } catch is CancellationError {
        } catch {
            ownerFailed = true
            owner = SettingsOwner()
        }
    }

    var goalsMax: Int { (owner?.maxChars ?? 0) > 0 ? owner!.maxChars : 4000 }
    var goalsDirty: Bool { goalsDraft != (owner?.goals ?? "") }

    /// Re-seeds from what the engine stored, not from what was sent: the write strips and
    /// trims to the ceiling, so the box shows the shorter reality.
    // from static/app/settings.js:83-99
    func saveGoals() async {
        guard let client, goalsDirty, !goalsBusy else { return }
        goalsBusy = true
        defer { goalsBusy = false }
        do {
            let d = try await client.post(
                "/api/owner/goals", body: ["goals": .string(goalsDraft)] as [String: JSONValue], as: SettingsGoalsSaved.self)
            var o = owner ?? SettingsOwner()
            o.goals = d.goals
            owner = o
            goalsDraft = d.goals
            flash(d.truncated ? "Saved, trimmed to \(Display.number(goalsMax)) characters" : "Saved")
            await changed()
        } catch is CancellationError {
        } catch {
            flash(LedgerSettings.sentence(error))
        }
    }

    func revertGoals() { goalsDraft = owner?.goals ?? "" }

    // MARK: The figures about this Mac, and the meaning search

    func loadDiagnostics() async {
        guard let client else { return }
        do {
            diagnostics = try await client.get("/api/diagnostics", as: SettingsDiagnostics.self)
            diagnosticsFailed = false
        } catch is CancellationError {
        } catch {
            diagnosticsFailed = true
        }
        watchInstall()
    }

    var semantic: SettingsSemanticStatus? { diagnostics?.semanticSearch }

    /// Poll while an install runs; stop the moment it settles, and tell the page around it
    /// so a row that said "Installing" catches up.
    // from static/app/settings.js:174-187
    private func watchInstall() {
        let running = semantic?.install.state == "running"
        if wasInstalling && !running { Task { await changed() } }
        wasInstalling = running
        if running {
            // One read at a time: each read schedules the next while the install runs.
            guard pollTask == nil else { return }
            pollTask = Task { [weak self] in
                try? await Task.sleep(for: .seconds(2.5))
                guard let self, !Task.isCancelled else { return }
                self.pollTask = nil
                await self.loadDiagnostics()
            }
        } else {
            pollTask?.cancel()
            pollTask = nil
        }
    }

    func stopPolling() {
        pollTask?.cancel()
        pollTask = nil
    }

    /// Downloads and installs about 2 GB (PyTorch) into the engine's own Python. The size
    /// is said on the button before any click.
    // from static/app/settings.js:189-198
    func installSemantic() async {
        guard let client, !semanticBusy else { return }
        semanticBusy = true
        defer { semanticBusy = false }
        do {
            let r = try await client.post("/api/semantic/install", body: [String: JSONValue](), as: SettingsSemanticStarted.self)
            if let status = r.status, var d = diagnostics {
                d.semanticSearch = status
                diagnostics = d
            }
            flash(r.message.isEmpty ? "Installing…" : r.message)
            watchInstall()
            await changed()
        } catch is CancellationError {
        } catch {
            flash(LedgerSettings.sentence(error))
        }
    }

    // MARK: Set up from scratch again

    /// Takes over the app until you are done; nothing you imported is removed.
    // from static/app/settings.js:266-285
    func setUpAgain() async {
        guard let client, let app, !setupBusy else { return }
        setupBusy = true
        defer { setupBusy = false }
        do {
            _ = try await client.post(
                "/api/onboarding", body: ["done": .bool(false)] as [String: JSONValue], as: OnboardingState.self)
            app.showOnboarding = true
        } catch is CancellationError {
        } catch {
            flash(LedgerSettings.sentence(error))
        }
    }
}

/// The models the paid features can use. Keep in step with the ANTHROPIC_MODEL default in
/// crm/settings.py: a default missing here would leave the picker showing nothing chosen.
// from static/app/settings.js:33-37
enum SettingsModels {
    static let options: [(value: String, label: String)] = [
        ("claude-opus-5-5", "Opus 5.5 — most capable"),
        ("claude-sonnet-5-5", "Sonnet 5.5 — balanced speed & smarts, half the price"),
        ("claude-haiku-5-5", "Haiku 5.5 — fastest, cheapest"),
        ("claude-opus-5", "Opus 5 — older, costs more than 5.5"),
    ]

    /// The list, with the stored value first when it is not one of these.
    static func choices(current: String) -> [(value: String, label: String)] {
        if current.isEmpty || options.contains(where: { $0.value == current }) { return options }
        return [(current, current)] + options
    }
}

// MARK: - Going to Settings from elsewhere

extension AppModel {
    /// Show the Settings page, opened on one of its rows and scrolled to it. Row keys are
    /// the engine's (GET /api/settings/ledger): "ai" holds the Anthropic key and the model,
    /// "identity" your name and email, "goals" what you are working on, "semantic" the
    /// meaning search. nil opens the page at its top. The web page's
    /// `#settings?row=<key>` link (static/app/ledger.js:44-70).
    func openSettings(row: String? = nil) {
        DataSettingsBus.shared.settingsRow = row
        section = .settings
    }
}
