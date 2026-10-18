import SwiftUI

// Full Disk Access, explained inside the app: the one macOS permission Messages, WhatsApp,
// Contacts and Calendar sit behind, which nothing can ask for. The web page's version is
// static/app/fda.js (FdaPanel); every state, sentence and button below comes from it.
//
// What the sheet can do, which is the whole design (fda.js:14-21): install the helper,
// take the person to the right System Settings pane with the helper ready in Finder, and
// say afterwards, per source, whether it worked. It also always says which of the two
// ways in is live (rule 6 in docs/EDITING.md): the helper, or copying the file yourself.

struct FullDiskAccessSheet: View {
    @AppEnvironment private var app
    @State private var store: FullDiskAccessStore

    init(store: FullDiskAccessStore = FullDiskAccessStore()) {
        _store = State(initialValue: store)
    }

    var body: some View {
        @Bindable var store = store
        SetupSheetChrome(
            title: "Full Disk Access",
            subtitle: "The one macOS permission your message history sits behind. Nothing can ask for it, so this sheet walks you to it and then tells you whether it worked.",
            size: CGSize(width: 620, height: 660),
            notices: store.notice.map { [$0] } ?? []
        ) {
            if store.busy != .none { ProgressView().controlSize(.small) }
        } content: {
            if let status = store.status {
                FullDiskAccessStateView(store: store, status: status)
                // The installer's own words, right under the buttons that ran it.
                if !store.output.isEmpty {
                    DisclosureGroup(isExpanded: $store.showOutput) {
                        SetupOutputBox(text: store.output).padding(.top, 6)
                    } label: {
                        Text(store.showOutput ? "Hide details" : "Show details")
                            .font(.system(size: Metrics.fsUI))
                            .foregroundStyle(Palette.muted)
                    }
                }
                FullDiskAccessRoutes(status: status)
            } else if let error = store.loadError {
                VStack(alignment: .leading, spacing: 8) {
                    Text("The helper's status did not load")
                        .font(.system(size: Metrics.fsTitle, weight: .semibold))
                    Text(error)
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.muted)
                    Button("Try Again") { Task { await store.load() } }
                }
            } else {
                ProgressView().controlSize(.small).frame(maxWidth: .infinity)
            }
        }
        .task { await store.start(app: app) }
        .confirmationDialog("Stop the helper?", isPresented: $store.confirmTurnOff) {
            Button("Turn It Off", role: .destructive) { Task { await store.uninstall() } }
            Button("Keep It", role: .cancel) {}
        } message: {
            // fda.js:88-89: the sentence says the copies stay, so nobody declines for fear
            // of losing their data.
            Text("What it already copied stays, and the app goes back to reading the copies you make yourself.")
        }
    }
}

// MARK: - The panel, every state of it (fda.js:304-518)

private struct FullDiskAccessStateView: View {
    let store: FullDiskAccessStore
    let status: FullDiskAccessStatus

    var body: some View {
        switch status.state {
        case "unavailable":
            FullDiskAccessHead(
                symbol: "shippingbox",
                title: "This copy of Network CRM has no sync helper",
                markdown: "It was built without the small program that copies these files for you, so automatic copying cannot be set up from here. Everything still works the other way: copy the file yourself in Finder and import it on the Data page.")
        case "not_installed":
            FullDiskAccessOffer(store: store, status: status)
        case "outdated":
            FullDiskAccessOutdated(store: store, status: status)
        case "ok", "absent", "quiet":
            FullDiskAccessWorking(store: store, status: status)
        default:
            FullDiskAccessBlocked(store: store, status: status)
        }
    }
}

/// Not opted in yet. An offer, not a warning (fda.js:313-345).
private struct FullDiskAccessOffer: View {
    let store: FullDiskAccessStore
    let status: FullDiskAccessStatus

    var body: some View {
        let copies = SetupWords.nameList(status.copies).isEmpty ? "your messages" : SetupWords.nameList(status.copies)
        VStack(alignment: .leading, spacing: 12) {
            FullDiskAccessHead(
                symbol: "clock",
                title: "Keep \(copies) up to date by themselves",
                markdown: "A copy you paste or export is a snapshot — it goes stale the moment something changes. One small helper can refresh them every couple of minutes. It needs one macOS permission, **Full Disk Access**, because for most of these macOS offers nothing narrower. **Network CRM itself never gets that permission** and never reads the protected files. Setting it up is two steps, both from here.")
            Button {
                Task { await store.install() }
            } label: {
                Label(store.busy == .install ? "Setting up…" : "Set up automatic updates", systemImage: "arrow.down.circle")
            }
            .buttonStyle(.borderedProminent)
            .disabled(store.busy != .none)
            SetupRichText(
                markdown: "This installs one small compiled program that copies those files and does nothing else — no network access of any kind. Granting the permission stays your own act in System Settings; this sheet will take you to the right pane and then tell you whether it worked.",
                size: Metrics.fsOverline + 1, color: Palette.muted)
            // NC-324 (fda.js): the permission protects the originals, not what the app keeps.
            SetupRichText(
                markdown: FullDiskAccessWords.storedPlainly,
                size: Metrics.fsOverline + 1, color: Palette.muted)
        }
    }
}

/// Installed, but older than this build: copying fewer sources, or copying all of them
/// with an older program than the one inside this app (NC-223) (fda.js:355-395).
private struct FullDiskAccessOutdated: View {
    let store: FullDiskAccessStore
    let status: FullDiskAccessStatus

    static let regrant = "Setting up again also means **re-granting Full Disk Access**: macOS ties that permission to the exact program, and this replaces it with a newer one. The next screen shows you how."

    var body: some View {
        let names = status.missing.joined(separator: " and ")
        VStack(alignment: .leading, spacing: 12) {
            if names.isEmpty {
                FullDiskAccessHead(
                    symbol: "arrow.down.circle",
                    title: "A newer sync helper came with this version of the app",
                    markdown: "The one installed still works and keeps copying. \(status.newerSays) \(Self.regrant)")
            } else {
                FullDiskAccessHead(
                    symbol: "arrow.down.circle",
                    title: "The sync helper needs setting up again to include \(names)",
                    markdown: "It is installed and running, but it is an older copy that does not know about \(names) yet — so \(names) will not update on its own until you set it up again. Everything it already syncs keeps working meanwhile. \(Self.regrant)")
            }
            if !status.sources.isEmpty { FullDiskAccessSourceList(sources: status.sources) }
            HStack(spacing: 8) {
                Button {
                    Task { await store.install() }
                } label: {
                    Label(store.busy == .install ? "Setting up…" : "Set it up again", systemImage: "arrow.down.circle")
                }
                .buttonStyle(.borderedProminent)
                .disabled(store.busy != .none)
                FullDiskAccessCheckButton(store: store)
                FullDiskAccessTurnOffButton(store: store)
            }
        }
    }
}

/// Working (or nothing to copy, or gone quiet): worth confirming, not explaining
/// (fda.js:387-427). The sheet adds the per-source lines the web page keeps for the
/// states that need them, since a sheet opened on purpose has room for them.
private struct FullDiskAccessWorking: View {
    let store: FullDiskAccessStore
    let status: FullDiskAccessStatus

    var body: some View {
        let ok = status.state == "ok"
        let read = FullDiskAccessWords.readLine(status)
        VStack(alignment: .leading, spacing: 12) {
            SetupCard(tone: ok ? .ok : .warn) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Image(systemName: ok ? "checkmark.circle.fill" : "info.circle.fill")
                        .foregroundStyle(ok ? Palette.green : Palette.gold)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(FullDiskAccessWords.workingHead(status))
                            .font(.system(size: Metrics.fsUI, weight: .medium))
                            .foregroundStyle(Palette.ink)
                            .fixedSize(horizontal: false, vertical: true)
                        if !read.isEmpty {
                            Text(read)
                                .font(.system(size: Metrics.fsOverline + 1).monospacedDigit())
                                .foregroundStyle(Palette.muted)
                        }
                    }
                }
            }
            if !status.sources.isEmpty { FullDiskAccessSourceList(sources: status.sources) }
            HStack(spacing: 8) {
                FullDiskAccessCheckButton(store: store)
                FullDiskAccessTurnOffButton(store: store)
                if store.checked, status.ranAgoSeconds != nil {
                    Text("checked \(FullDiskAccessWords.ago(status.ranAgoSeconds))")
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.muted)
                }
            }
        }
    }
}

/// macOS is refusing, or the helper has never reported: the full explanation
/// (fda.js:432-518).
private struct FullDiskAccessBlocked: View {
    let store: FullDiskAccessStore
    let status: FullDiskAccessStatus

    var body: some View {
        let blocked = status.state == "blocked"
        VStack(alignment: .leading, spacing: 14) {
            FullDiskAccessHead(
                symbol: "lock",
                title: blocked ? "macOS is blocking \(status.blocked.joined(separator: " and "))" : "The sync helper has not reported yet",
                markdown: blocked
                    ? "Your message history needs one macOS permission called **Full Disk Access**, and nothing can ask for it — not this app, not macOS. It has to be granted by hand, once, to the one small helper below. **Network CRM itself never gets it.**"
                    : "It is installed but has not run yet, or it is an older copy that cannot report back. Run it now to see the real answer.")
            if !status.sources.isEmpty { FullDiskAccessSourceList(sources: status.sources) }
            if blocked { FullDiskAccessSteps(store: store, path: status.binary) }
            HStack(spacing: 8) {
                Button {
                    store.openSettings()
                } label: {
                    Label("Open Full Disk Access settings", systemImage: "lock.open")
                }
                .buttonStyle(.borderedProminent)
                Button {
                    store.revealHelper()
                } label: {
                    Label("Show the helper in Finder", systemImage: "folder")
                }
            }
            Divider().overlay(Palette.line)
            VStack(alignment: .leading, spacing: 6) {
                HStack(spacing: 8) {
                    FullDiskAccessCheckButton(store: store)
                    // Re-running the installer is safe and sometimes the fix; it never rebuilds
                    // an unchanged helper, because a new binary loses the grant (fda.js:502-505).
                    Button(store.busy == .install ? "Reinstalling…" : "Reinstall the helper") {
                        Task { await store.install() }
                    }
                    .disabled(store.busy != .none)
                    FullDiskAccessTurnOffButton(store: store)
                }
                .fixedSize()
                if store.checked, status.ranAgoSeconds != nil {
                    Text("checked \(FullDiskAccessWords.ago(status.ranAgoSeconds))")
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.muted)
                }
            }
        }
    }
}

/// The four numbered steps for a blocked helper, with its path in step 2 (fda.js:452-494).
private struct FullDiskAccessSteps: View {
    let store: FullDiskAccessStore
    let path: String

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            step(1) {
                SetupRichText(markdown: "Open **Full Disk Access** in System Settings — the button below this list goes straight there.")
            }
            step(2) {
                VStack(alignment: .leading, spacing: 6) {
                    SetupRichText(markdown: "Click **+**, then drag in (or select) this one file — \"Show the helper in Finder\" opens the folder with it already picked:")
                    HStack(spacing: 8) {
                        Text(path)
                            .font(.system(size: Metrics.fsOverline, design: .monospaced))
                            .foregroundStyle(Palette.ink)
                            .lineLimit(2)
                            .truncationMode(.middle)
                            .textSelection(.enabled)
                            .padding(.horizontal, 8)
                            .padding(.vertical, 5)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(RoundedRectangle(cornerRadius: Metrics.radiusPill).fill(Palette.bg))
                            .help(path)
                        Button("Copy path") { store.copyPath() }
                    }
                }
            }
            step(3) {
                SetupRichText(markdown: "Make sure its toggle is **on**. If macOS says the program has to be quit before this takes effect, you can ignore it — the helper runs for a few seconds at a time and has already stopped.")
            }
            step(4) {
                SetupRichText(markdown: "Come back here and press **Check now**. It will say whether it worked, rather than leaving you to guess.")
            }
            SetupRichText(
                markdown: "Grant it to **that program only** — not to Terminal, and not to Network CRM. It is one small compiled program whose only job is to copy those databases into a folder this app can already read; it has no network access of any kind. Its source code is published with the project, in `packaging/src/imessage_sync.swift`.",
                size: Metrics.fsOverline + 1, color: Palette.muted)
        }
        .padding(14)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.panel2))
    }

    private func step<C: View>(_ n: Int, @ViewBuilder _ content: () -> C) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(verbatim: "\(n)")
                .font(.system(size: Metrics.fsOverline, weight: .semibold).monospacedDigit())
                .foregroundStyle(Palette.muted)
                .frame(width: 16, alignment: .trailing)
            content()
        }
    }
}

// MARK: - Parts

private struct FullDiskAccessHead: View {
    let symbol: String
    let title: String
    let markdown: String

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 18, weight: .regular))
                .foregroundStyle(Palette.muted)
                .frame(width: 24)
            VStack(alignment: .leading, spacing: 6) {
                Text(title)
                    .font(.system(size: Metrics.fsTitle, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                SetupRichText(markdown: markdown, color: Palette.ink)
            }
        }
    }
}

/// Per source: icon, name, and what the helper said about it (fda.js:136-149).
private struct FullDiskAccessSourceList: View {
    let sources: [FullDiskAccessSource]

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ForEach(sources) { s in
                let o = FullDiskAccessWords.outcome(s.outcome)
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Circle().fill(o.tone.color).frame(width: 7, height: 7)
                    Image(systemName: FullDiskAccessWords.symbol(s.label))
                        .font(.system(size: 12))
                        .foregroundStyle(Palette.muted)
                        .frame(width: 16)
                    Text(s.label)
                        .font(.system(size: Metrics.fsUI, weight: .medium))
                        .foregroundStyle(Palette.ink)
                        .frame(width: 78, alignment: .leading)
                    Text(FullDiskAccessWords.sourceState(s))
                        .font(.system(size: Metrics.fsUI).monospacedDigit())
                        .foregroundStyle(s.outcome == "blocked" ? Palette.red : Palette.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .accessibilityElement(children: .combine)
            }
        }
    }
}

private struct FullDiskAccessCheckButton: View {
    let store: FullDiskAccessStore
    var body: some View {
        Button {
            Task { await store.recheck() }
        } label: {
            Label(store.busy == .check ? "Checking…" : "Check now", systemImage: "arrow.clockwise")
        }
        .disabled(store.busy != .none)
        .help("Run the helper now, the way the scheduled refresh does, and read its report")
    }
}

/// "Turn off automatic copying" asks first (fda.js:74-95), as a Mac confirmation.
private struct FullDiskAccessTurnOffButton: View {
    let store: FullDiskAccessStore
    var body: some View {
        Button(store.busy == .uninstall ? "Turning off…" : "Turn off automatic copying…") {
            store.confirmTurnOff = true
        }
        .disabled(store.busy != .none)
    }
}

/// The two ways in, always both on screen, with which one is live (rule 6 in
/// docs/EDITING.md, NC-123). The switch is the helper itself, but which route a source is on is read
/// per source, the way the Data page labels it: only a source the helper's last run
/// copied or found current is on the helper (importers.auto_copy_label); a blocked one
/// is still read from the copy you make yourself.
private struct FullDiskAccessRoutes: View {
    let status: FullDiskAccessStatus
    @AppEnvironment private var app

    var body: some View {
        let live = FullDiskAccessWords.liveRoutes(status)
        let copies = SetupWords.nameList(live.all)
        VStack(alignment: .leading, spacing: 10) {
            SetupSectionLabel(text: "Two ways in")
            route(
                title: "Automatic copying, with the helper",
                tag: FullDiskAccessWords.helperTag(status, live),
                text: "One small helper program holds Full Disk Access and copies \(copies) into the app's own folder every couple of minutes. You grant it once in System Settings, then there is nothing to do. Network CRM itself never gets the permission.")
            route(
                title: "Copying the file yourself, with no permission",
                tag: FullDiskAccessWords.ownCopyTag(live),
                text: "Copy the file in Finder, or export a .vcf or .ics, and import that copy on the Data page. Every source the helper covers keeps a row there that says how. Turning the helper off falls back to this on its own.")
            HStack(spacing: 6) {
                Text(FullDiskAccessWords.liveLine(live))
                    .font(.system(size: Metrics.fsOverline + 1))
                    .foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
                Spacer(minLength: 4)
                if app.section != .data {
                    Button("Open Data") {
                        app.section = .data
                        app.setupFlow = nil
                    }
                    .controlSize(.small)
                }
            }
        }
        .padding(.top, 4)
    }

    private func route(title: String, tag: (String, SetupTone), text: String) -> some View {
        SetupCard {
            HStack(spacing: 8) {
                Text(title)
                    .font(.system(size: Metrics.fsUI, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                Spacer(minLength: 4)
                SetupTag(text: tag.0, tone: tag.1)
            }
            Text(text)
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.muted)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}
