import SwiftUI

// Gmail's setup as a thing somebody can finish (NC-117). The web page's version is
// static/app/gmail.js (GmailSetup); its three steps are this sheet's three parts:
//
//   1. the address   — which mailbox, saved as the GMAIL_ACCOUNTS setting
//   2. the file      — what Google gives you once, from its Cloud Console
//   3. the sign-in   — a person clicking Allow in a Google tab, the only step that waits

struct GmailSheet: View {
    @AppEnvironment private var app
    @State private var store: GmailStore

    init(store: GmailStore = GmailStore()) {
        _store = State(initialValue: store)
    }

    var body: some View {
        SetupSheetChrome(
            title: "Gmail",
            subtitle: "Read the conversations in one Google mailbox. Three steps: the address, the sign-in file Google gives you, and signing in to Google once.",
            size: CGSize(width: 640, height: 680),
            notices: store.notice.map { [$0] } ?? []
        ) {
            if store.adding || store.busyEmail != nil { ProgressView().controlSize(.small) }
        } content: {
            if let status = store.status {
                GmailSetupBody(store: store, status: status)
            } else if let error = store.loadError {
                VStack(alignment: .leading, spacing: 8) {
                    Text(error)
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.muted)
                        .fixedSize(horizontal: false, vertical: true)
                    Button("Try Again") { Task { await store.load() } }
                }
            } else {
                ProgressView().controlSize(.small).frame(maxWidth: .infinity)
            }
        }
        .task { await store.start(app: app) }
        .onDisappear { store.stop() }
        .confirmationDialog(
            "Stop collecting from \(store.confirmRemove?.email ?? "this address")?",
            isPresented: Binding(get: { store.confirmRemove != nil }, set: { if !$0 { store.confirmRemove = nil } }),
            presenting: store.confirmRemove
        ) { row in
            Button("Remove Address", role: .destructive) { Task { await store.removeAccount(row.email) } }
            Button("Cancel", role: .cancel) {}
        } message: { _ in
            Text("The address comes off the list Gmail reads. Mail already imported stays, and the sign-in file stays on this Mac in case you add the address again.")
        }
    }
}

private struct GmailSetupBody: View {
    let store: GmailStore
    let status: GmailStatus

    var body: some View {
        let rows = status.accounts
        let connect = status.connect
        let running = connect.state == "running"
        let minutes = max(1, Int((Double(status.connectTimeoutSec > 0 ? status.connectTimeoutSec : 600) / 60).rounded()))
        VStack(alignment: .leading, spacing: 16) {
            if !rows.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(rows) { row in GmailAccountRow(store: store, row: row, connect: connect) }
                }
            }

            if running {
                SetupCard(tone: .warn) {
                    HStack(alignment: .top, spacing: 10) {
                        Image(systemName: "arrow.up.forward.app")
                            .foregroundStyle(Palette.gold)
                        VStack(alignment: .leading, spacing: 4) {
                            Text("A Google tab is open — choose Allow there.")
                                .font(.system(size: Metrics.fsUI, weight: .semibold))
                            Text("Nothing is happening on this Mac until you do. Take as long as you like; the app gives up after \(minutes) minutes and you can start again.")
                                .font(.system(size: Metrics.fsUI))
                                .foregroundStyle(Palette.muted)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    }
                }
            }

            if connect.state == "failed" { GmailFailed(connect: connect) }

            if connect.state == "succeeded" && !running && !connect.message.isEmpty {
                SetupLine(text: connect.message, tone: .ok, symbol: "checkmark.circle")
            }

            // The steps are here whenever an address is, open by default only while the
            // file is still missing, so they are a wall of text exactly once (gmail.js:339-370).
            if !rows.isEmpty {
                GmailSteps(store: store, startOpen: !status.needsFile.isEmpty)
            }

            GmailAddAccount(store: store, existing: !rows.isEmpty)

            if rows.isEmpty {
                SetupRichText(
                    markdown: "Or skip all of this: the **Email (.mbox)** source on the Data page imports a Google Takeout export of your mail with no account access at all.",
                    size: Metrics.fsUI, color: Palette.muted)
            }
        }
    }
}

/// One configured address and whatever it is still missing (gmail.js:154-235). One
/// prominent button, and it is whichever thing is actually needed.
private struct GmailAccountRow: View {
    let store: GmailStore
    let row: GmailAccount
    let connect: GmailConnect

    var body: some View {
        let state = GmailRowState(row, connect: connect)
        let waiting = state == .waiting
        let busy = store.busyEmail != nil
        SetupCard(tone: state.tone == .plain ? nil : state.tone) {
            HStack(spacing: 8) {
                Circle().fill(state.tone == .plain ? Palette.faint : state.tone.color).frame(width: 8, height: 8)
                Text(verbatim: row.email)
                    .font(.system(size: Metrics.fsUI, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                    .lineLimit(1)
                    .truncationMode(.middle)
                    .textSelection(.enabled)
                Spacer(minLength: 8)
                Text(state.says)
                    .font(.system(size: Metrics.fsUI))
                    .foregroundStyle(state.tone == .plain ? Palette.muted : state.tone.color)
                Menu {
                    Button("Remove This Address…") { store.confirmRemove = row }
                } label: {
                    Image(systemName: "ellipsis")
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .disabled(busy || waiting)
                .help("More for this address")
            }
            HStack(spacing: 8) {
                if !row.hasCredentials {
                    Button(store.busyEmail == row.email ? "Choosing…" : "Choose the file Google gave you…") {
                        Task { await store.pickFile(for: row.email) }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(busy)
                }
                if row.hasCredentials && (!row.hasToken || row.signInExpired) {
                    Button(waiting ? "Waiting for Google…" : row.signInExpired ? "Sign in again" : "Sign in to Google") {
                        Task { await store.signIn(row.email) }
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(busy || waiting)
                }
                if row.hasCredentials {
                    Button(row.hasToken ? "Replace the file…" : "Choose a different file…") {
                        Task { await store.pickFile(for: row.email) }
                    }
                    .disabled(busy)
                }
                if row.hasToken && !row.signInExpired && !waiting {
                    Button("Sign in again") { Task { await store.signIn(row.email) } }
                        .disabled(busy)
                }
            }
            .controlSize(.small)
        }
    }
}

private struct GmailFailed: View {
    let connect: GmailConnect
    @State private var open = false

    var body: some View {
        SetupCard(tone: .bad) {
            Text(connect.message)
                .font(.system(size: Metrics.fsUI, weight: .semibold))
                .foregroundStyle(Palette.ink)
                .fixedSize(horizontal: false, vertical: true)
            if !connect.tail.isEmpty {
                DisclosureGroup(isExpanded: $open) {
                    SetupOutputBox(text: connect.tail.joined(separator: "\n")).padding(.top, 4)
                } label: {
                    Text("what Google said")
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.muted)
                }
            }
        }
    }
}

/// The five steps through Google's console, as data because the wording is the
/// deliverable (gmail.js:53-80), with the warnings that go with them (gmail.js:85-90, 361-369).
private struct GmailSteps: View {
    let store: GmailStore
    @State private var open: Bool

    init(store: GmailStore, startOpen: Bool) {
        self.store = store
        _open = State(initialValue: startOpen)
    }

    static let steps = [
        "**Make a project.** Open the Google Cloud Console with the button above and make a new project — any name will do, it is only a folder for one app's settings and it costs nothing.",
        "**Turn on the Gmail API** for that project: **APIs and Services** → **Library**, search for **Gmail API**, open it and press **Enable**. Skip this and the sign-in still works — it is the reading of mail afterwards that fails, a confusing place to end up.",
        "**Say what the app is called.** In the left menu open **Google Auth Platform** (Google renamed this from \"OAuth consent screen\"), fill in a name and your own address under **Branding**. Then open the tab called **Audience** and choose **External** — a personal Google account has no company to be Internal to, so it is the only choice offered.",
        "**Press Publish app, on that same Audience page.** This is the step every other guide skips and the one that decides whether you do this once or every week: while it still says **Testing**, Google throws your permission away after seven days and this app will ask you to sign in again next Monday, and the Monday after that. Published, it lasts.",
        "**Make the sign-in file.** Still in Google Auth Platform, open the **Clients** tab and press **Create client**. For the type, pick the one called **Desktop app** — not Web application: a web client has nowhere to send you back to on this Mac, and Google refuses it at sign-in with an error about a mismatched address. Press **Create**, then download the JSON file it offers. It lands in Downloads under a long name that starts with `client_secret_` — leave that name alone, the button above renames and files it for you.",
    ]

    var body: some View {
        DisclosureGroup(isExpanded: $open) {
            VStack(alignment: .leading, spacing: 10) {
                Button {
                    store.openConsole()
                } label: {
                    Label("Open Google Cloud Console", systemImage: "arrow.up.forward.app")
                }
                SetupRichText(
                    markdown: "Google will not let one app read many people's mail without a yearly audit, so each person makes their own key. It is five minutes, once.",
                    color: Palette.muted)
                ForEach(Array(Self.steps.enumerated()), id: \.offset) { i, step in
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text(verbatim: "\(i + 1)")
                            .font(.system(size: Metrics.fsOverline, weight: .semibold).monospacedDigit())
                            .foregroundStyle(Palette.muted)
                            .frame(width: 16, alignment: .trailing)
                        SetupRichText(markdown: step)
                    }
                }
                SetupRichText(
                    markdown: "The first time you press Sign in, Google will say it has not verified this app. That is expected: the app is the one you made a minute ago, on your own account, and it never leaves this Mac — nobody reviewed it because there is nobody else to protect from it. Choose **Advanced**, then the **Go to …** link underneath, then **Allow**.",
                    color: Palette.muted)
                SetupRichText(
                    markdown: "**Signing in again every week?** Step 4 is why. Go back to Google Auth Platform → Audience and check that it says **In production** and not **Testing**; Testing deletes the permission after seven days, every time.",
                    color: Palette.muted)
            }
            .padding(.top, 8)
        } label: {
            Text("How to make the sign-in file Google asks for")
                .font(.system(size: Metrics.fsUI, weight: .medium))
                .foregroundStyle(Palette.ink)
        }
    }
}

/// Add an address to GMAIL_ACCOUNTS (gmail.js:103-152). Return adds. (The web page's Esc
/// clears the field; in a Mac sheet Esc closes the sheet, so it does that instead.)
private struct GmailAddAccount: View {
    let store: GmailStore
    let existing: Bool
    @FocusState private var focused: Bool

    var body: some View {
        @Bindable var store = store
        let value = store.draft.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        VStack(alignment: .leading, spacing: 6) {
            Text(existing ? "Add another Google address" : "Which Google address?")
                .font(.system(size: Metrics.fsUI, weight: .semibold))
                .foregroundStyle(Palette.ink)
            HStack(spacing: 8) {
                TextField("Google address", text: $store.draft, prompt: Text(verbatim: "you@gmail.com"))
                    .textFieldStyle(.roundedBorder)
                    .autocorrectionDisabled()
                    .focused($focused)
                    .disabled(store.adding)
                    .onSubmit { Task { await store.addAccount() } }
                    .frame(maxWidth: 320)
                Button(store.adding ? "Saving…" : "Add") { Task { await store.addAccount() } }
                    .disabled(!GmailStore.looksLikeEmail(value) || store.adding)
            }
            Text("The mailbox you want read. It has to be the address you actually sign in to Google with.")
                .font(.system(size: Metrics.fsOverline + 1))
                .foregroundStyle(Palette.muted)
        }
        .onAppear { if !existing { focused = true } }
    }
}
