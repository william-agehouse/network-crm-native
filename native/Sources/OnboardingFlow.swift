import SwiftUI

// First-run setup, a sheet over the window (static/app/onboarding.js): what this is, who you
// are, LinkedIn, Apple Contacts, iMessage, remember three people, an optional AI key. Nothing is required and
// "Skip setup" is always there. RootView shows it while `app.showOnboarding` is true;
// OnboardingGate decides that on launch, and Help > Set Up Network CRM… opens it again.
//
// Keys: Return presses the step's main button, Escape is Skip setup, ⌘[ goes back.
// The steps themselves are in OnboardingSteps.swift, the engine calls in OnboardingStore.

struct OnboardingFlow: View {
    @AppEnvironment private var app
    @State private var store = OnboardingStore()

    static let size = CGSize(width: 600, height: 640)

    var body: some View {
        VStack(spacing: 0) {
            topBar
            ScrollView {
                stepView
                    .padding(.horizontal, 44)
                    .padding(.top, 4)
                    .padding(.bottom, 24)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
            .scrollContentBackground(.hidden)
            Divider().overlay(Palette.line)
            progress
        }
        .frame(width: Self.size.width, height: Self.size.height)
        .background(Palette.panel)
        .overlay(alignment: .bottom) {
            if let t = store.toast {
                CmdToast(text: t)
                    .padding(.bottom, 64)
                    .transition(.opacity)
            }
        }
        .animation(.easeOut(duration: 0.2), value: store.toast)
        .onAppear {
            store.attach(app)
            takeSnapshotHint()
        }
    }

    @ViewBuilder private var stepView: some View {
        switch store.step {
        case .hello: OnbHelloStep(store: store)
        case .you: OnbYouStep(store: store)
        case .linkedin: OnbLinkedInStep(store: store)
        case .contacts: OnbContactsStep(store: store, step: store.contacts)
        case .imessage: OnbMessagesStep(store: store, step: store.imessage)
        case .remember: OnbRememberStep(store: store)
        case .ai: OnbAIStep(store: store)
        }
    }

    /// "Skip setup", always visible (onboarding.js:595). It finishes setup, like the web page.
    private var topBar: some View {
        HStack {
            Spacer()
            Button("Skip setup") { Task { await store.finish() } }
                .buttonStyle(.borderless)
                .foregroundStyle(Palette.muted)
                .keyboardShortcut(.cancelAction)
                .disabled(store.finishing)
                .help("Close setup. Everything here is also on the Data and Settings pages.")
        }
        .padding(.horizontal, 16)
        .frame(height: 40)
    }

    /// Back, the dots and "Step 2 of 7" (onboarding.js:601-613).
    private var progress: some View {
        HStack(spacing: Metrics.gap) {
            Button {
                store.back()
            } label: {
                Label("Back", systemImage: "chevron.left")
            }
            .controlSize(.small)
            .keyboardShortcut("[", modifiers: .command)
            .opacity(store.step == .hello ? 0 : 1)
            .disabled(store.step == .hello)
            .frame(width: 90, alignment: .leading)
            Spacer(minLength: 0)
            HStack(spacing: 6) {
                ForEach(OnboardingStore.Step.allCases, id: \.rawValue) { s in
                    Circle()
                        .fill(s.rawValue < store.step.rawValue ? Palette.green
                              : s == store.step ? Palette.accent : Palette.lineStrong)
                        .frame(width: 7, height: 7)
                }
            }
            .accessibilityElement(children: .ignore)
            .accessibilityLabel("Setup progress")
            .accessibilityValue("Step \(store.step.rawValue + 1) of \(OnboardingStore.total)")
            Text("Step \(store.step.rawValue + 1) of \(OnboardingStore.total)")
                .font(.system(size: Metrics.fsOverline).monospacedDigit())
                .foregroundStyle(Palette.muted)
            Spacer(minLength: 0)
            Color.clear.frame(width: 90, height: 1)
        }
        .padding(.horizontal, 16)
        .frame(height: 48)
    }

    /// `--snapshot`: "onboarding" opens on the first step, "onboarding-<step>" on that one.
    private func takeSnapshotHint() {
        guard let hint = app.snapshotHint, hint.hasPrefix("onboarding") else { return }
        app.snapshotHint = nil
        switch hint {
        case "onboarding-you": store.step = .you
        case "onboarding-linkedin": store.step = .linkedin
        case "onboarding-contacts": store.step = .contacts
        case "onboarding-imessage": store.step = .imessage
        case "onboarding-remember": store.step = .remember
        case "onboarding-ai": store.step = .ai
        default: store.step = .hello
        }
    }
}

/// Decides on launch whether to show first-run setup. It asks GET /api/settings/ledger
/// (`install.setup_again.done`) rather than GET /api/onboarding, which used to WRITE: on its
/// first call it stored the "already set up" flag for an install with many people. NC-342
/// made that GET a plain read, so either would do now; the ledger is kept because it
/// answers the same on an engine from before that change.
@MainActor
enum OnboardingGate {
    static func check(_ app: AppModel) async {
        if app.snapshotHint?.hasPrefix("onboarding") == true {
            app.showOnboarding = true
            return
        }
        // Development engines carry someone's real data or a scratch copy: never pop a
        // wizard over them by itself.
        guard !app.engine.isDevelopmentPort else { return }
        // The engine moved an unreadable database aside and started an empty one: setup
        // would import into the empty one before the banner saying so had been read.
        guard app.engine.status?.dbRecovery.isEmpty ?? true else { return }
        if await needsSetup(app.client) { app.showOnboarding = true }
    }

    /// true when this Mac has neither finished nor skipped setup. A failure counts as done,
    /// as on the web page (main.js:657-659): a wizard must never trap someone out of
    /// their own data because a status line could not be read.
    static func needsSetup(_ client: EngineClient) async -> Bool {
        do {
            let ledger = try await client.get("/api/settings/ledger", as: OnbSettingsLedger.self, timeout: 15)
            return !ledger.install.setupAgain.done
        } catch {
            return false
        }
    }
}
