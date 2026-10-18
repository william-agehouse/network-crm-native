import AppKit
import SwiftUI

// The seven steps of first-run setup, word for word from static/app/onboarding.js. Each
// step calls `app.markSettled()` once its content is on screen, for `--snapshot`.

// MARK: - Shared pieces

/// A step's title (`.onb-h2`).
private struct OnbHeading: View {
    let text: Text
    init(_ s: String) { text = Text(s) }
    init(text: Text) { self.text = text }
    var body: some View {
        text
            .font(.system(size: Metrics.fsPage, weight: .semibold))
            .kerning(-0.3)
            .foregroundStyle(Palette.ink)
            .fixedSize(horizontal: false, vertical: true)
            .accessibilityAddTraits(.isHeader)
    }
}

/// Body copy (`.onb-sub`).
private struct OnbSub: View {
    let text: Text
    init(_ s: String) { text = Text(s) }
    init(text: Text) { self.text = text }
    var body: some View {
        text
            .font(.system(size: Metrics.fsUI))
            .foregroundStyle(Palette.muted)
            .lineSpacing(3)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// The quiet line under a step (`.onb-quiet-line`, `.onb-privacy`). Muted rather than the
/// web page's faint: it carries meaning, and faint is for decoration only (Theme.swift).
private struct OnbQuiet: View {
    let text: String
    init(_ s: String) { text = s }
    var body: some View {
        Text(text)
            .font(.system(size: Metrics.fsOverline))
            .foregroundStyle(Palette.muted)
            .fixedSize(horizontal: false, vertical: true)
    }
}

private struct OnbSectionLabel: View {
    let text: String
    init(_ s: String) { text = s }
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: Metrics.fsOverline, weight: .semibold))
            .kerning(0.4)
            .foregroundStyle(Palette.muted)
    }
}

/// The square "n" mark, or a tick on the done screen (`.onb-mark`).
private struct OnbMark: View {
    var symbol: String? = nil
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: Metrics.radius).fill(Palette.accent)
            if let symbol {
                Image(systemName: symbol).font(.system(size: 20, weight: .bold))
            } else {
                Text(verbatim: "n").font(.system(size: Metrics.fsPage, weight: .bold))
            }
        }
        .foregroundStyle(.white)
        .frame(width: 44, height: 44)
        .accessibilityHidden(true)
    }
}

/// A box around a group, the web page's `.card` / `.onb-found-row`.
private struct OnbBox<Content: View>: View {
    var padding: CGFloat = Metrics.gap
    @ViewBuilder let content: Content
    var body: some View {
        content
            .padding(padding)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: Metrics.radius).fill(Palette.panel2))
            .overlay(RoundedRectangle(cornerRadius: Metrics.radius).strokeBorder(Palette.line))
    }
}

extension View {
    /// The step's main button: filled, as the web page's `.btn.primary`, and in the same
    /// prominent style as Ask's send button.
    func onbPrimary() -> some View {
        buttonStyle(.borderedProminent).tint(Palette.accent)
    }
}

// MARK: - Step 1: what this is

struct OnbHelloStep: View {
    let store: OnboardingStore
    @AppEnvironment private var app

    var body: some View {
        VStack(spacing: Metrics.gap) {
            OnbMark().padding(.bottom, 4)
            Text("Network CRM")
                .font(.system(size: Metrics.fsPage, weight: .semibold))
                .kerning(-0.3)
                .foregroundStyle(Palette.ink)
            Text("This app remembers your network: who each person is, how you met, and when you last spoke.")
                .font(.system(size: Metrics.fsTitle))
                .foregroundStyle(Palette.muted)
                .multilineTextAlignment(.center)
                .lineSpacing(3)
                .frame(maxWidth: 420)
                .fixedSize(horizontal: false, vertical: true)
            VStack(alignment: .leading, spacing: Metrics.gapSmall) {
                fact("What it needs.", "A small helper that copies your messages, contacts and calendar for you. You give it Full Disk Access in System Settings; this app itself never gets it. Or, if you would rather grant nothing, the files you copy or export yourself.")
                fact("What it does.", "It reads those files, scores every relationship, and keeps the notes you write about people.")
                fact("What stays here.", "All of it. Your messages live in a database on this Mac. The app uploads nothing.")
            }
            .frame(maxWidth: 440, alignment: .leading)
            .padding(.top, 4)
            OnbQuiet("Setup takes \(OnboardingStore.total) steps. You can skip any of them.")
                .padding(.top, 4)
            Button("Set it up") { store.next() }
                .keyboardShortcut(.defaultAction)
                .onbPrimary()
                .controlSize(.large)
                .padding(.top, 4)
        }
        .frame(maxWidth: .infinity)
        .padding(.top, 20)
        .onAppear { app.markSettled() }
    }

    private func fact(_ label: String, _ text: String) -> some View {
        OnbSub(text: Text("\(Text(label).fontWeight(.semibold).foregroundStyle(Palette.ink)) \(text)"))
    }
}

// MARK: - Step 2: who you are (NC-318)

struct OnbYouStep: View {
    @Bindable var store: OnboardingStore
    @AppEnvironment private var app

    var body: some View {
        let typed = !store.selfName.trimmingCharacters(in: .whitespaces).isEmpty
            || !store.selfEmail.trimmingCharacters(in: .whitespaces).isEmpty
        VStack(alignment: .leading, spacing: Metrics.gap) {
            OnbHeading("Who you are")
            OnbSub("Your name, and the email address you write from. The app uses them to tell your side of a conversation from everyone else's, and drafts it writes use your name instead of calling you \u{201C}the user\u{201D}.")
            OnbQuiet("Both are kept in this app's settings on this Mac.")
            VStack(alignment: .leading, spacing: 6) {
                OnbSectionLabel("Your name")
                TextField("Ada Lovelace", text: $store.selfName)
                    .textFieldStyle(.roundedBorder)
                    .textContentType(.name)
                    .accessibilityLabel("Your name")
                OnbSectionLabel("Your email address").padding(.top, 4)
                TextField("ada@example.com", text: $store.selfEmail)
                    .textFieldStyle(.roundedBorder)
                    .textContentType(.emailAddress)
                    .autocorrectionDisabled()
                    .onSubmit { Task { await store.saveYou() } }
                    .accessibilityLabel("Your email address")
                if !store.youNote.isEmpty { LedgerFieldNote(text: store.youNote) }
            }
            .padding(.top, 4)
            Button(store.youBusy ? "Saving\u{2026}" : typed ? "Save and continue" : "Continue") {
                Task { await store.saveYou() }
            }
            .keyboardShortcut(.defaultAction)
            .onbPrimary()
            .controlSize(.large)
            .disabled(store.youBusy)
            .padding(.top, 4)
            if !typed { OnbQuiet("You can set both later in Settings.") }
        }
        .task {
            await store.loadYou()
            app.markSettled()
        }
    }
}

// MARK: - Step 3: LinkedIn (NC-319)

struct OnbLinkedInStep: View {
    let store: OnboardingStore
    @AppEnvironment private var app

    /// onboarding.js LINKEDIN_STEPS, the numbered layout every source's steps use.
    private static let steps = """
        1. Press **Open LinkedIn's data page**. It opens in your browser.
        2. Choose **Connections** only. That is the file that arrives first.
        3. Press **Request archive**. LinkedIn may ask for your password.
        4. Come back here and press **I have asked for it**.
        """

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.gap) {
            OnbHeading("LinkedIn")
            OnbSub("LinkedIn sends a copy of your connections by email, usually within a day or two of asking. Ask now, so it is on its way while you set up the rest.")
            OnbBox { CmdMarkdown(text: Self.steps) }
            HStack(spacing: Metrics.gapSmall) {
                Button {
                    NSWorkspace.shared.open(OnboardingStore.linkedInDataPage)
                } label: {
                    Label("Open LinkedIn's data page", systemImage: "arrow.up.right.square")
                }
                .controlSize(.large)
                Button {
                    Task { await store.markLinkedInAsked() }
                } label: {
                    if store.linkedInAsked { Label("Asked", systemImage: "checkmark") } else { Text("I have asked for it") }
                }
                .onbPrimary()
                .controlSize(.large)
                .disabled(store.linkedInBusy || store.linkedInAsked)
            }
            if store.linkedInAsked {
                OnbSub("When the email comes, download and unzip the file, then choose Connections.csv on the LinkedIn connections row of the Data page. Until then, the People page says it is waiting.")
            }
            OnbQuiet("This app sends nothing to LinkedIn. You ask in your own browser, and the file LinkedIn sends stays on this Mac. LinkedIn messages and profile facts take more setup, and wait on the Data page.")
            // NC-320, said on the step before the first one that looks for files on this Mac.
            OnbQuiet("Next, the app looks on this Mac for files to import, so macOS may ask whether \u{201C}python3\u{201D} or \u{201C}Python\u{201D} may read your Desktop, Downloads or WhatsApp's folder. That is this app's own engine asking, not another program, and your answer affects only that one source.")
            Button("Continue") { store.next() }
                .keyboardShortcut(.defaultAction)
                .onbPrimary()
                .controlSize(.large)
                .padding(.top, 4)
        }
        .task {
            await store.loadLinkedIn()
            app.markSettled()
        }
    }
}

// MARK: - Steps 4 and 5: Apple Contacts, then iMessage

struct OnbContactsStep: View {
    let store: OnboardingStore
    let step: OnbImportStep
    @AppEnvironment private var app

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.gap) {
            OnbHeading("Apple Contacts")
            OnbSub("Import your address book first. iMessage does not need this to work — it can find names on its own. But a contact already on this list arrives complete: phone, email, company, title. A name iMessage creates by itself has none of that.")
            OnbQuiet("This reads the sync helper's copy of your address book if it has made one, and otherwise only the card file you pick. It writes what it reads to a database on this Mac.")
            OnbFoundList(store: store, step: step)
            OnbSourceRun(store: store, step: step)
            Button("Continue") { store.continueFromContacts() }
                .keyboardShortcut(.defaultAction)
                .onbPrimary()
                .controlSize(.large)
                .padding(.top, 4)
        }
        .task {
            await store.loadImportStep(step)
            app.markSettled()
        }
    }
}

struct OnbMessagesStep: View {
    let store: OnboardingStore
    let step: OnbImportStep
    @AppEnvironment private var app

    var body: some View {
        VStack(alignment: .leading, spacing: Metrics.gap) {
            OnbHeading("iMessage")
            OnbSub("This step imports your iMessage history. It matches each message to a person by phone number or email. A person already in your address book — imported in the step before this — arrives with more than just a name.")
            // NC-193: the simple route first, above the copy-it-yourself steps.
            OnbHelperBox(onChanged: { Task { await store.detect(step) } })
            OnbFoundList(store: store, step: step)
            OnbSourceRun(store: store, step: step)
            OnbQuiet("WhatsApp, email and more wait in Data.")
            Button("Continue") { store.next() }
                .keyboardShortcut(.defaultAction)
                .onbPrimary()
                .controlSize(.large)
                .padding(.top, 4)
        }
        .task {
            await store.loadImportStep(step)
            app.markSettled()
        }
    }
}

/// What GET /api/fda/status says about the sync helper, the parts this box reads.
struct OnbFdaStatus: Codable, Sendable, Hashable {
    /// unavailable, not_installed, no_report, blocked, outdated, absent, quiet, ok.
    @Def var state: String
    /// What this build's helper copies ("iMessage", "WhatsApp", "Contacts", "Calendar").
    @Def var copies: [String]
    @Def var missing: [String]
}

/// The sync helper, as the wizard's iMessage step offers it (onboarding.js:322-355, the
/// Data page's FdaPanel). Installing, granting and checking live in the Full Disk Access
/// sheet (SetupFlowView), which this opens on top; closing it looks for copies again.
/// The status is read only on the real engine: on a development port the box makes the
/// offer without asking, because the engine's answer comes from files in ~/Library.
private struct OnbHelperBox: View {
    let onChanged: () -> Void
    @AppEnvironment private var app
    @State private var status: OnbFdaStatus?
    @State private var showSheet = false

    var body: some View {
        Group {
            if status?.state != "unavailable" {
                OnbBox {
                    VStack(alignment: .leading, spacing: Metrics.gapSmall) {
                        HStack(alignment: .firstTextBaseline, spacing: 8) {
                            Image(systemName: symbol).foregroundStyle(tone)
                            Text(headline)
                                .font(.system(size: Metrics.fsUI, weight: .semibold))
                                .foregroundStyle(Palette.ink)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                        if let text = detail {
                            OnbSub(text: text)
                        }
                        Button(buttonTitle) { showSheet = true }
                    }
                }
            }
        }
        .sheet(isPresented: $showSheet, onDismiss: {
            Task { await load() }
            onChanged()
        }) {
            SetupFlowView(flow: .fullDiskAccess).environment(app)
        }
        .task { await load() }
    }

    private func load() async {
        guard !app.engine.isDevelopmentPort else { return }
        status = try? await app.client.get("/api/fda/status", as: OnbFdaStatus.self)
    }

    private var state: String { status?.state ?? "not_installed" }

    private var symbol: String {
        switch state {
        case "ok", "absent", "quiet": "checkmark.circle"
        case "blocked", "outdated", "no_report": "exclamationmark.circle"
        default: "clock"
        }
    }

    private var tone: Color {
        switch state {
        case "ok": Palette.green
        case "blocked": Palette.danger
        case "outdated", "no_report", "quiet": Palette.gold
        default: Palette.muted
        }
    }

    /// The panel's own headlines (fda.js:316-420), shortened to one line each.
    private var headline: String {
        switch state {
        case "ok": "The sync helper keeps your copies up to date."
        case "absent": "The sync helper found none of the apps it copies from on this Mac."
        case "quiet": "The sync helper has not reported for a while."
        case "blocked": "macOS is refusing the sync helper, so nothing new arrives."
        case "outdated":
            (status?.missing ?? []).isEmpty
                ? "A newer sync helper came with this version of the app."
                : "The sync helper needs setting up again to include \(Self.nameList(status?.missing ?? []))."
        case "no_report": "The sync helper is installed but has not reported yet."
        default:
            "Keep \(Self.nameList(status?.copies ?? []).isEmpty ? "your messages" : Self.nameList(status?.copies ?? [])) up to date by themselves"
        }
    }

    private var detail: Text? {
        guard state == "not_installed" else { return nil }
        return Text("A copy you paste or export is a snapshot — it goes stale the moment something changes. One small helper can refresh them every couple of minutes. It needs one macOS permission, \(Text("Full Disk Access").fontWeight(.semibold)), because for most of these macOS offers nothing narrower. \(Text("Network CRM itself never gets that permission").fontWeight(.semibold)) and never reads the protected files.")
    }

    private var buttonTitle: String {
        state == "not_installed" ? "Set Up Automatic Updates…" : "Open Full Disk Access Settings…"
    }

    /// "a", "a and b", "a, b and c" (fda.js:57-62).
    static func nameList(_ names: [String]) -> String {
        guard let last = names.last else { return "" }
        return names.count == 1 ? last : names.dropLast().joined(separator: ", ") + " and " + last
    }
}

/// "Found on this Mac": copies the engine detected, each with its own Import button
/// (onboarding.js:124-156, :231-238).
private struct OnbFoundList: View {
    let store: OnboardingStore
    let step: OnbImportStep

    var body: some View {
        if let found = step.found, !found.isEmpty {
            VStack(alignment: .leading, spacing: Metrics.gapSmall) {
                OnbSectionLabel("Found on this Mac")
                ForEach(found) { f in row(f) }
            }
        }
    }

    private func row(_ f: OnbFinding) -> some View {
        let busy = step.findingBusy.contains(f.id)
        let result = step.findingResult[f.id]
        let done = result?.ok == true
        return OnbBox(padding: 10) {
            HStack(spacing: Metrics.gap) {
                CmdGlyph(symbol: f.source == "imessage" ? "message" : "person.crop.circle", size: 28)
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 4) {
                        Text(verbatim: f.label).font(.system(size: Metrics.fsUI, weight: .semibold))
                        if let count = OnboardingStore.countLabel(f) {
                            Text(count).font(.system(size: Metrics.fsUI).monospacedDigit()).foregroundStyle(Palette.muted)
                        }
                    }
                    if !f.mtime.isEmpty {
                        Text(verbatim: f.mtime).font(.system(size: Metrics.fsOverline)).foregroundStyle(Palette.muted)
                    }
                    if let result {
                        Text(verbatim: result.line)
                            .font(.system(size: Metrics.fsOverline))
                            .foregroundStyle(result.ok ? Palette.green : Palette.danger)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                }
                Spacer(minLength: 8)
                Button {
                    Task { await store.runFinding(f, in: step) }
                } label: {
                    if done {
                        Label("Imported", systemImage: "checkmark")
                    } else {
                        Text(busy ? "Importing…" : "Import")
                    }
                }
                .disabled(busy || done)
            }
        }
    }
}

/// The source's own help, its Finder buttons, the picker and path box, and Run the import
/// (onboarding.js:240-256, :366-382). Hidden while the engine's source list is missing,
/// as on the web page.
private struct OnbSourceRun: View {
    let store: OnboardingStore
    @Bindable var step: OnbImportStep

    var body: some View {
        if let spec = step.spec {
            VStack(alignment: .leading, spacing: Metrics.gap) {
                if !spec.exportHelp.isEmpty {
                    OnbBox { CmdMarkdown(text: spec.exportHelp) }
                }
                if !helpers(spec).isEmpty {
                    HStack(spacing: Metrics.gapSmall) {
                        ForEach(helpers(spec), id: \.key) { h in
                            Button {
                                Task { await store.open(h) }
                            } label: {
                                Label(h.label, systemImage: "arrow.up.forward.app")
                            }
                        }
                    }
                }
                if spec.kind != "none" {
                    HStack(spacing: Metrics.gapSmall) {
                        Button(step.picking ? "Waiting for the picker…"
                               : spec.kind == "folder" ? "Choose a folder" : "Choose a file") {
                            Task { await store.pick(step) }
                        }
                        .disabled(step.picking)
                        // NC-178: always there, so a picker that cannot open still leaves a way in.
                        TextField(spec.kind == "folder" ? "…or paste the folder's path here" : "…or paste the file's path here",
                                  text: $step.path)
                            .textFieldStyle(.roundedBorder)
                            .accessibilityLabel(spec.kind == "folder" ? "Path to the folder" : "Path to the file")
                            .onSubmit { Task { await store.runImport(step) } }
                    }
                }
                HStack(spacing: Metrics.gapSmall) {
                    Button(step.running ? "Importing…" : "Run the import") {
                        Task { await store.runImport(step) }
                    }
                    .disabled(!step.canRun)
                    if step.running { ProgressView().controlSize(.small) }
                }
                if let line = step.runLine {
                    Text(verbatim: line)
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(step.runOK ? Palette.green : Palette.danger)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
        }
    }

    /// The source's Finder buttons, or the older single `open_helper` (ui.js:37-40).
    private func helpers(_ spec: OnbSource) -> [OnbOpenHelper] {
        if !spec.openHelpers.isEmpty { return spec.openHelpers }
        guard !spec.openHelper.isEmpty else { return [] }
        var h = OnbOpenHelper()
        h.key = spec.key
        h.label = spec.openHelper
        return [h]
    }
}

// MARK: - Step 4: remember three people

struct OnbRememberStep: View {
    @Bindable var store: OnboardingStore
    @AppEnvironment private var app

    var body: some View {
        content
            .task {
                await store.loadQueue()
                app.markSettled()
            }
    }

    @ViewBuilder private var content: some View {
        if let queue = store.queue {
            if queue.isEmpty {
                // Zero data is a real state on a fresh Mac, not a failure (onboarding.js:484-496).
                VStack(alignment: .leading, spacing: Metrics.gap) {
                    OnbHeading("Remember three people")
                    OnbBox(padding: Metrics.padView) {
                        VStack(alignment: .leading, spacing: 6) {
                            Text("Nobody to review yet")
                                .font(.system(size: Metrics.fsUI, weight: .semibold))
                                .foregroundStyle(Palette.muted)
                            Text("Import a source first. This step then fills itself.")
                                .font(.system(size: Metrics.fsUI))
                                .foregroundStyle(Palette.muted)
                            Text("This step picks three people worth remembering once your network is in.")
                                .font(.system(size: Metrics.fsOverline))
                                .foregroundStyle(Palette.muted)
                        }
                    }
                    continueButton
                }
            } else if let person = store.currentPerson {
                VStack(alignment: .leading, spacing: Metrics.gap) {
                    OnbHeading("Remember three people")
                    OnbSub("Write one line about each person. The app shows you that line the next time the name comes up.")
                    OnbReviewCard(store: store, person: person, index: store.queueIndex, total: queue.count)
                        .id(person.id)
                }
            } else {
                VStack(spacing: Metrics.gap) {
                    OnbMark(symbol: "checkmark")
                    OnbHeading("\(store.rememberedCount) \(Display.noun(store.rememberedCount, "person", "people")) remembered")
                    OnbSub("That is the habit: a few minutes a day, a few people at a time.")
                        .multilineTextAlignment(.center)
                    continueButton
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, Metrics.padView)
            }
        } else {
            VStack(alignment: .leading, spacing: Metrics.gap) {
                OnbHeading("Remember three people")
                HStack(spacing: 8) {
                    ProgressView().controlSize(.small)
                    OnbSub("Picking three people…")
                }
            }
        }
    }

    private var continueButton: some View {
        Button("Continue") { store.next() }
            .keyboardShortcut(.defaultAction)
                .onbPrimary()
            .controlSize(.large)
            .padding(.top, 4)
    }
}

/// One person to describe (onboarding.js:391-449).
private struct OnbReviewCard: View {
    @Bindable var store: OnboardingStore
    let person: OnbQueuePerson
    let index: Int
    let total: Int
    @AppEnvironment private var app
    @FocusState private var focused: Bool

    var body: some View {
        let role = Display.roleString(position: person.position, company: person.company)
        let first = OnboardingStore.firstName(person.name)
        OnbBox(padding: Metrics.padView) {
            VStack(alignment: .leading, spacing: Metrics.gap) {
                HStack(spacing: Metrics.gap) {
                    CmdAvatar(name: person.name, photo: person.photo, size: 64, ring: true,
                              strength: person.strength, label: person.strengthLabel, client: app.client)
                    VStack(alignment: .leading, spacing: 4) {
                        Text(verbatim: person.name)
                            .font(.system(size: Metrics.fsTitle, weight: .semibold))
                            .foregroundStyle(Palette.ink)
                            .lineLimit(2)
                        if !role.isEmpty {
                            Text(verbatim: role)
                                .font(.system(size: Metrics.fsUI))
                                .foregroundStyle(Palette.muted)
                                .lineLimit(2)
                        }
                        let tags = CmdTag.tags(metro: person.homeMetro, city: person.homeCity, role: person.roleType,
                                               topics: person.topics)
                        if !tags.isEmpty {
                            HStack(spacing: 4) {
                                ForEach(Array(tags.enumerated()), id: \.offset) { _, t in CmdTag(text: t.0, help: t.1) }
                            }
                        }
                    }
                    Spacer(minLength: 0)
                }
                if let last = person.lastMessages.last {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(OnboardingStore.lastExchangeMeta(last))
                            .font(.system(size: Metrics.fsOverline, weight: .medium))
                            .foregroundStyle(Palette.muted)
                        Text(verbatim: OnboardingStore.quote(last.body))
                            .font(.system(size: Metrics.fsUI))
                            .foregroundStyle(Palette.ink)
                            .lineSpacing(2)
                            .fixedSize(horizontal: false, vertical: true)
                    }
                    .padding(.leading, 10)
                    .overlay(alignment: .leading) {
                        Rectangle().fill(Palette.lineStrong).frame(width: 2)
                    }
                }
                VStack(alignment: .leading, spacing: 6) {
                    Text("How do you know \(first)?")
                        .font(.system(size: Metrics.fsUI, weight: .semibold))
                        .foregroundStyle(Palette.ink)
                    TextField("Where you met. What they do. What you want to remember.", text: $store.noteText,
                              axis: .vertical)
                        .lineLimit(2...5)
                        .textFieldStyle(.roundedBorder)
                        .focused($focused)
                        .onSubmit(save)
                        .accessibilityLabel("How do you know \(first)?")
                }
                HStack(spacing: Metrics.gapSmall) {
                    Button("Save", action: save)
                        .keyboardShortcut(.defaultAction)
                        .onbPrimary()
                        .disabled(store.reviewBusy || store.noteText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                    Button("Skip") { Task { await store.skip() } }
                        .disabled(store.reviewBusy)
                    Spacer()
                    Text("\(index + 1) of \(total)")
                        .font(.system(size: Metrics.fsOverline).monospacedDigit())
                        .foregroundStyle(Palette.muted)
                }
            }
        }
        .onAppear { focused = true }
    }

    private func save() {
        Task { await store.saveNote() }
    }
}

// MARK: - Step 5: an AI key (optional)

struct OnbAIStep: View {
    @Bindable var store: OnboardingStore
    @AppEnvironment private var app
    @FocusState private var focused: Bool

    var body: some View {
        let hasKey = !store.apiKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        VStack(alignment: .leading, spacing: Metrics.gap) {
            OnbHeading(text: Text("Add an AI key \(Text("(optional)").fontWeight(.medium).foregroundStyle(Palette.muted))"))
            OnbSub("An Anthropic API key turns on Ask, story rewrites and message drafts. Everything else works without a key.")
            // The disclosure reads at the same weight as the value claim (onboarding.js:545-547).
            OnbSub("Ask sends your question to Anthropic. To answer it, Ask can also send names, notes and the message text that matches. Your database stays on this Mac.")
            VStack(alignment: .leading, spacing: 6) {
                OnbSectionLabel("Anthropic API key")
                SecureField("sk-ant-…", text: $store.apiKey)
                    .textFieldStyle(.roundedBorder)
                    .focused($focused)
                    .onSubmit { Task { await store.saveKeyAndFinish() } }
                    .accessibilityLabel("Anthropic API key")
                OnbSub(LedgerWords.anthropicSpendHint)
            }
            .padding(.top, 4)
            HStack(spacing: Metrics.gapSmall) {
                Button(hasKey ? "Save the key and finish" : "Finish") {
                    Task { await store.saveKeyAndFinish() }
                }
                .keyboardShortcut(.defaultAction)
                .onbPrimary()
                .controlSize(.large)
                .disabled(store.finishing)
                if hasKey {
                    Button("Finish without a key") { Task { await store.finish() } }
                        .controlSize(.large)
                        .disabled(store.finishing)
                }
                if store.finishing { ProgressView().controlSize(.small) }
            }
            OnbQuiet("You can add or remove the key later in Settings.")
        }
        .onAppear {
            app.markSettled()
        }
    }
}
