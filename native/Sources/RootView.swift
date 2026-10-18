import AppKit
import SwiftUI

// The window: a sidebar (People, Ask, Lists; then Data and Settings; the engine's status at
// the bottom) and the chosen screen. Until the engine answers it shows a calm progress line;
// if it cannot be reached it says why and offers Retry. The screens and sheets are referred
// to by name only (PeopleScreen, DataScreen, CommandBar, MergeSheet…), so other files can
// replace them whole.

struct RootView: View {
    @AppEnvironment private var app
    /// A confirmation from somewhere other than the screen on show (a merge from a person's
    /// page, ⌘K's Copy, File › Export a Backup…): the web page's toast.
    @State private var flashText: String?
    @State private var flashTask: Task<Void, Never>?

    var body: some View {
        @Bindable var app = app
        NavigationSplitView {
            Sidebar()
                .navigationSplitViewColumnWidth(min: 180, ideal: 210, max: 280)
        } detail: {
            VStack(spacing: 0) {
                if app.engine.isReady {
                    if let trouble = app.engine.trouble { EngineTroubleBanner(trouble: trouble) }
                    if let notice = app.engine.status?.dbRecovery, !notice.isEmpty {
                        DatabaseRecoveryBanner(notice: notice)
                    }
                }
                content
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Palette.panel)
            .overlay(alignment: .bottom) {
                if let flashText { LedgerFlash(text: flashText).transition(.opacity) }
            }
            .animation(.easeOut(duration: 0.2), value: flashText)
        }
        .task { await app.start() }
        // Data and Settings show these lines themselves (DataScreen, SettingsScreen).
        .onChange(of: DataSettingsBus.shared.announceTick) {
            guard !app.section.isTool, let line = DataSettingsBus.shared.announcement else { return }
            flash(line)
        }
        .sheet(isPresented: $app.showCommandBar) { CommandBar().environment(app) }
        .sheet(isPresented: $app.showOnboarding) { OnboardingFlow().environment(app) }
        .sheet(item: $app.mergeRequest) { request in MergeSheet(request: request).environment(app) }
        .sheet(item: $app.setupFlow) { flow in SetupFlowView(flow: flow).environment(app) }
    }

    private func flash(_ line: String) {
        flashText = line
        NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested,
                             userInfo: [.announcement: line, .priority: NSAccessibilityPriorityLevel.high.rawValue])
        flashTask?.cancel()
        flashTask = Task {
            try? await Task.sleep(for: .seconds(2.6))
            guard !Task.isCancelled else { return }
            flashText = nil
        }
    }

    @ViewBuilder private var content: some View {
        switch app.engine.state {
        case .connecting:
            EngineWaitingView(sentence: "Opening Network CRM…")
        case .settingUp:
            EngineWaitingView(sentence: EngineWaitingView.settingUp)
        case .starting:
            EngineWaitingView(sentence: "Starting the Network CRM engine. This takes a few seconds.")
        case .failed(let failure):
            EngineFailedView(failure: failure)
        case .ready:
            switch app.section {
            case .people: PeopleScreen()
            case .ask: AskScreen()
            case .lists: ListsScreen()
            case .data: DataScreen()
            case .settings: SettingsScreen()
            }
        }
    }
}

private struct Sidebar: View {
    @AppEnvironment private var app

    var body: some View {
        // Rows draw their own selection instead of using List(selection:): the system's
        // sidebar highlight is a window-server material, which an offscreen --snapshot
        // captures as a black bar with the label invisible on it.
        List {
            ForEach(AppModel.Section.allCases.filter { !$0.isTool }) { row($0) }
            Spacer().frame(height: 10).listRowSeparator(.hidden)
            ForEach(AppModel.Section.allCases.filter(\.isTool)) { row($0) }
        }
        .listStyle(.sidebar)
        .safeAreaInset(edge: .bottom, spacing: 0) {
            EngineStatusLine()
        }
    }

    private func row(_ section: AppModel.Section) -> some View {
        SidebarRow(section: section, count: count(for: section), selected: app.section == section) {
            app.section = section
        }
        .listRowInsets(EdgeInsets(top: 1, leading: 8, bottom: 1, trailing: 8))
    }

    private func count(for section: AppModel.Section) -> Int {
        switch section {
        case .people: app.peopleCount ?? 0
        case .ask: app.chatCount ?? 0
        case .lists: app.listCount ?? 0
        case .data, .settings: 0
        }
    }
}

private struct SidebarRow: View {
    let section: AppModel.Section
    let count: Int
    let selected: Bool
    let action: () -> Void
    @State private var hovering = false

    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                Image(systemName: section.symbol)
                    .frame(width: 18)
                    .foregroundStyle(selected ? Palette.accentInk : Palette.muted)
                Text(section.title)
                    .foregroundStyle(selected ? Palette.accentInk : Palette.ink)
                    .fontWeight(selected ? .semibold : .regular)
                Spacer(minLength: 4)
                if count > 0 {
                    Text(Display.number(count))
                        .font(.system(size: Metrics.fsOverline).monospacedDigit())
                        .foregroundStyle(Palette.muted)
                }
            }
            .font(.system(size: Metrics.fsUI))
            .padding(.horizontal, 8)
            .frame(height: 28)
            .contentShape(Rectangle())
            .background(
                RoundedRectangle(cornerRadius: Metrics.radiusSmall)
                    .fill(selected ? Palette.accentSoft : hovering ? Palette.hover : .clear))
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .accessibilityAddTraits(selected ? .isSelected : [])
    }
}

/// One line at the foot of the sidebar: is the engine there, and where.
struct EngineStatusLine: View {
    @AppEnvironment private var app

    var body: some View {
        let (text, color) = describe(app.engine.state)
        HStack(spacing: 6) {
            Circle().fill(color).frame(width: 7, height: 7)
            Text(text)
                .font(.system(size: Metrics.fsOverline))
                .foregroundStyle(Palette.muted)
                .lineLimit(1)
            Spacer(minLength: 0)
            if app.engine.isDevelopmentPort, app.engine.port != 0 {
                Text(verbatim: ":\(app.engine.port)")
                    .font(.system(size: Metrics.fsOverline).monospacedDigit())
                    .foregroundStyle(Palette.faint)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 10)
        .help(app.engine.isReady ? "Runs on this Mac. Your data stays here." : "Server unreachable")
    }

    private func describe(_ state: EngineController.State) -> (String, Color) {
        switch state {
        case .connecting: ("Opening…", Palette.faint)
        case .settingUp: ("Setting up…", Display.Staleness.warm.color)
        case .starting: ("Starting the engine…", Display.Staleness.warm.color)
        case .ready: app.engine.trouble == nil ? ("Runs on this Mac", Palette.green) : ("Engine not answering", Palette.danger)
        case .failed: ("Engine unreachable", Palette.danger)
        }
    }
}

/// Above every screen while the engine misses its status checks. The screens stay, so
/// nothing typed is lost; the banner goes by itself when the engine answers again.
struct EngineTroubleBanner: View {
    let trouble: EngineController.Failure
    @AppEnvironment private var app
    @State private var checking = false

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: "bolt.horizontal.circle").foregroundStyle(Palette.danger)
            VStack(alignment: .leading, spacing: 2) {
                Text(trouble.message).fontWeight(.semibold).foregroundStyle(Palette.ink)
                Text(trouble.advice).foregroundStyle(Palette.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 8)
            Button(checking ? "Checking…" : "Check Now") {
                checking = true
                Task {
                    await app.refreshCounts()
                    checking = false
                }
            }
            .disabled(checking)
        }
        .font(.system(size: Metrics.fsUI))
        .padding(.horizontal, Metrics.padView)
        .padding(.vertical, 10)
        .background(Palette.redSoft)
        .overlay(alignment: .bottom) { Divider().overlay(Palette.line) }
        .accessibilityElement(children: .combine)
    }
}

/// The engine found crm.db unreadable at start, moved it aside and began an empty one
/// (crm/app.py main()). Its sentence says where the old file went; shown until
/// "I've read this", as the web page's banner (main.js:706-714). The button asks the engine
/// to forget the notice with a POST (AppModel.dismissRecoveryNotice, NC-342); if that
/// fails, the banner stays and says why under its own sentence.
struct DatabaseRecoveryBanner: View {
    let notice: String
    @AppEnvironment private var app
    @State private var busy = false
    @State private var problem: String?

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Image(systemName: "info.circle").foregroundStyle(Palette.accentInk)
            VStack(alignment: .leading, spacing: 3) {
                Text(verbatim: notice)
                    .foregroundStyle(Palette.ink)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                if let problem {
                    Text(verbatim: problem)
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.danger)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 8)
            Button("I've read this") {
                busy = true
                Task {
                    problem = await app.dismissRecoveryNotice()
                    busy = false
                }
            }
            .disabled(busy)
        }
        .font(.system(size: Metrics.fsUI))
        .padding(.horizontal, Metrics.padView)
        .padding(.vertical, 10)
        .background(Palette.accentSoft)
        .overlay(alignment: .bottom) { Divider().overlay(Palette.line) }
        .accessibilityAddTraits(.isStaticText)
    }
}

struct EngineWaitingView: View {
    /// The first start of a new version, while crm/python_env.py installs the packages (NC-316).
    static let settingUp = "Setting up the optional parts (Ask with AI, Gmail, Google photos). This happens once, takes about a minute and needs the internet."
    let sentence: String
    var body: some View {
        VStack(spacing: 12) {
            ProgressView().controlSize(.small)
            Text(sentence)
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.muted)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

private struct EngineFailedView: View {
    let failure: EngineController.Failure
    @AppEnvironment private var app
    @State private var retrying = false

    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: "bolt.horizontal.circle")
                .font(.system(size: 28, weight: .light))
                .foregroundStyle(Palette.muted)
            Text(failure.message)
                .font(.system(size: Metrics.fsTitle, weight: .semibold))
                .foregroundStyle(Palette.ink)
                .multilineTextAlignment(.center)
            Text(failure.advice)
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.muted)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            Button(retrying ? "Retrying…" : "Retry") {
                retrying = true
                Task {
                    await app.retry()
                    retrying = false
                }
            }
            .disabled(retrying)
            .keyboardShortcut(.defaultAction)
            .padding(.top, 6)
        }
        .padding(Metrics.padView)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear { app.markSettled() }
    }
}

/// A calm placeholder the stub screens use until they are replaced. Lives here, not in a
/// stub, so replacing one stub file never breaks the others.
struct StubPlaceholder: View {
    let title: String
    let detail: String
    var body: some View {
        VStack(spacing: 8) {
            Text(title).font(.system(size: Metrics.fsPage, weight: .semibold)).foregroundStyle(Palette.ink)
            Text(detail).font(.system(size: Metrics.fsUI)).foregroundStyle(Palette.muted)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Palette.panel)
    }
}
