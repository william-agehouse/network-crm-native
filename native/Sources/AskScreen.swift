import AppKit
import SwiftUI

// Ask: questions about your own network, kept as saved chats.
//
// Two panes: the saved chats on the left (newest first, New Chat and ⌘N, rename and delete
// from the context menu or the ⋯ menu), the open chat on the right with the composer at
// its foot. The open chat is `app.selectedChatID`; nil means the newest one, and that nil
// is never rewritten into an id (contract §8.8). All state lives in AskModel, one per
// AppModel, so an answer still being worked out survives a trip to People and back.
// Words and rules follow static/app/ask.js; the file and line are beside each rule.

struct AskScreen: View {
    @AppEnvironment private var app

    var body: some View {
        AskScreenBody(model: AskModel.shared(for: app))
    }
}

private struct AskScreenBody: View {
    @AppEnvironment private var app
    @Bindable var model: AskModel

    var body: some View {
        HSplitView {
            AskChatsRail(model: model)
                .frame(minWidth: 210, idealWidth: 250, maxWidth: 360)
            AskConversation(model: model)
                .frame(minWidth: 440, maxWidth: .infinity)
                .layoutPriority(1)
        }
        .background(Palette.panel)
        .task { await model.appeared() }
        .onChange(of: app.selectedChatID) { Task { await model.loadTranscript() } }
        // Staged text (an intent from ⌘K, "Ask about…"): into the box, never sent.
        .onChange(of: app.askDraft, initial: true) { model.takeDraft() }
        // ⌘K "Start a new chat": New Chat once, then the request is cleared.
        .onChange(of: app.requestNewChat, initial: true) {
            if app.requestNewChat { Task { await model.handleNewChatRequest() } }
        }
        // Both dialogs hand their chat to the action: closing a dialog clears
        // `renaming`/`deleting` before a Task started by its button gets to run.
        .alert(
            "Rename Chat",
            isPresented: Binding(get: { model.renaming != nil }, set: { if !$0 { model.renaming = nil } }),
            presenting: model.renaming
        ) { chat in
            TextField("Chat name", text: $model.renameText)
                .onChange(of: model.renameText) {
                    let max = model.limits.titleMax
                    if max > 0, model.renameText.count > max { model.renameText = String(model.renameText.prefix(max)) }
                }
            Button("Save") {
                let typed = model.renameText
                Task { await model.commitRename(chat, to: typed) }
            }
            Button("Cancel", role: .cancel) { model.renaming = nil }
        } message: { _ in
            Text("A chat takes its name from your first question. Up to \(model.limits.titleMax) characters.")
        }
        .confirmationDialog(
            "Delete this chat?",
            isPresented: Binding(get: { model.deleting != nil }, set: { if !$0 { model.deleting = nil } }),
            presenting: model.deleting
        ) { chat in
            Button("Delete Chat", role: .destructive) { Task { await model.commitDelete(chat) } }
            Button("Cancel", role: .cancel) { model.deleting = nil }
        } message: { chat in
            // ask.js:1499-1502: the delete is soft, so the messages are not destroyed.
            let n = model.chats.first { $0.id == chat.id }?.turns ?? chat.turns ?? 0
            Text(n > 0 ? "The \(n) \(n == 1 ? "message stays" : "messages stay") on disk." : "It has no messages.")
        }
    }
}

// MARK: - Chats rail

private struct AskChatsRail: View {
    @AppEnvironment private var app
    let model: AskModel

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            if let a = model.adoption, a.adopted > 0, String(a.conversationID) != model.adoptionSeen {
                adoptionNote(a)
                Divider()
            }
            content
        }
        .background(Palette.panel2)
    }

    private var header: some View {
        HStack(spacing: 8) {
            Text(countLine)
                .font(.system(size: Metrics.fsOverline, weight: .medium))
                .foregroundStyle(Palette.muted)
                .monospacedDigit()
            Spacer()
            Button {
                Task { await model.newChat() }
            } label: {
                Label("New Chat", systemImage: "square.and.pencil")
            }
            .labelStyle(.titleAndIcon)
            .controlSize(.small)
            .keyboardShortcut("n", modifiers: .command)
            .help("Start a new chat (⌘N)")
        }
        .padding(.horizontal, 12)
        .frame(height: 44)
    }

    private var countLine: String {
        guard model.chatsState == .loaded, !model.chats.isEmpty else { return "Chats" }
        let n = model.chats.count
        return "\(Display.number(n)) \(n == 1 ? "chat" : "chats")"
    }

    @ViewBuilder private var content: some View {
        switch model.chatsState {
        case .loading:
            ProgressView().controlSize(.small).frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let message):
            AskCentered {
                Text("Ask could not load your chats.").foregroundStyle(Palette.ink)
                Text(message).foregroundStyle(Palette.muted)
                Button("Retry") { Task { await model.loadChats() } }.controlSize(.small)
            }
        case .loaded:
            if model.chats.isEmpty {
                // ask.js:707-711
                AskCentered {
                    Text("No chats yet").fontWeight(.semibold).foregroundStyle(Palette.muted)
                    Text("Send a question below. It starts your first chat.").foregroundStyle(Palette.muted)
                    Text("A chat takes its name from your first question.").foregroundStyle(Palette.faint)
                }
            } else {
                list
            }
        }
    }

    private var selection: Binding<Int?> {
        Binding(
            get: { app.selectedChatID ?? (model.convID == 0 ? nil : model.convID) },
            set: { model.select($0) })
    }

    private var list: some View {
        List(selection: selection) {
            ForEach(model.chats) { chat in
                AskChatRow(chat: chat, answering: model.pending?.chatID == chat.id)
                    .tag(chat.id)
                    .contextMenu {
                        Button("Rename…") { model.beginRename(chat) }
                        Button("Delete…", role: .destructive) { model.beginDelete(chat) }
                        Divider()
                        Button("New Chat") { Task { await model.newChat() } }
                    }
            }
        }
        .listStyle(.inset)
        .scrollContentBackground(.hidden)
        .onDeleteCommand {
            if let id = selection.wrappedValue, let chat = model.chats.first(where: { $0.id == id }) {
                model.beginDelete(chat)
            }
        }
        .onKeyPress(.return) {
            model.composerFocus += 1
            return .handled
        }
    }

    private func adoptionNote(_ a: ChatAdoption) -> some View {
        HStack(alignment: .top, spacing: 6) {
            Text(
                "Ask put \(a.adopted) earlier \(a.adopted == 1 ? "message" : "messages") in one chat. It is in the list below."
            )
            .font(.system(size: Metrics.fsOverline))
            .foregroundStyle(Palette.muted)
            .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            Button {
                model.dismissAdoption()
            } label: {
                Image(systemName: "xmark").font(.system(size: 9, weight: .semibold))
            }
            .buttonStyle(.borderless)
            .help("Hide this message")
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }
}

private struct AskChatRow: View {
    let chat: ChatRow
    let answering: Bool

    /// Never blank: an empty chat says so (ask.js:651-656).
    private var meta: String {
        let n = chat.turns ?? 0
        if n == 0 { return "No messages yet" }
        let last = (chat.lastMessageAt ?? "").isEmpty ? chat.updatedAt : chat.lastMessageAt ?? ""
        let when = Display.relDate(last)
        return "\(Display.number(n)) \(n == 1 ? "message" : "messages")" + (when.isEmpty ? "" : " · \(when)")
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text(chat.title.isEmpty ? "New chat" : chat.title)
                .font(.system(size: Metrics.fsUI, weight: .medium))
                .lineLimit(1)
                .truncationMode(.tail)
            HStack(spacing: 4) {
                if answering {
                    ProgressView().controlSize(.mini)
                    Text("Answering")
                } else {
                    Text(meta)
                }
            }
            .font(.system(size: Metrics.fsOverline))
            .foregroundStyle(.secondary)
            .lineLimit(1)
        }
        .padding(.vertical, 3)
        .help(chat.title)
    }
}

// MARK: - The open chat

private struct AskConversation: View {
    @AppEnvironment private var app
    @Bindable var model: AskModel
    @State private var composerHeight = AskComposerField.minHeight

    var body: some View {
        VStack(spacing: 0) {
            header
            Divider()
            transcript
            Divider()
            composer
        }
        .background(Palette.panel)
        .overlay(alignment: .bottom) { toast }
    }

    // MARK: Header

    private var title: String {
        if model.gone != nil { return "Chat not found" }
        if let c = model.conversation { return c.title.isEmpty ? "New chat" : c.title }
        return model.transcriptState == .loading ? "" : "New chat"
    }

    private var header: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 1) {
                Text(title)
                    .font(.system(size: Metrics.fsUI, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                    .lineLimit(1)
                    .truncationMode(.tail)
                if model.convID > 0 {
                    let n = model.currentTurnCount
                    Text(n == 0 ? "No messages yet" : "\(Display.number(n)) \(n == 1 ? "message" : "messages")")
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.muted)
                }
            }
            Spacer()
            if let chat = model.currentChatRow, model.convID > 0 {
                Menu {
                    Button("Rename…") { model.beginRename(chat) }
                    Button("Delete Chat…", role: .destructive) { model.beginDelete(chat) }
                } label: {
                    Image(systemName: "ellipsis.circle")
                }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .help("Rename or delete this chat")
            }
        }
        .padding(.horizontal, 16)
        .frame(height: 44)
    }

    // MARK: Transcript

    @ViewBuilder private var transcript: some View {
        switch model.transcriptState {
        case .loading:
            ProgressView().controlSize(.small).frame(maxWidth: .infinity, maxHeight: .infinity)
        case .failed(let message):
            AskCentered {
                Text("Ask could not open this chat.").fontWeight(.semibold).foregroundStyle(Palette.ink)
                Text(message).foregroundStyle(Palette.muted)
                Button("Retry") { Task { await model.loadTranscript() } }.controlSize(.small)
            }
        case .loaded:
            if let gone = model.gone {
                AskCentered {
                    AskNotice(
                        content: .init(tone: .bad, text: "Ask cannot open this chat. It may be deleted.", detail: gone),
                        actionTitle: "Start a new chat",
                        action: { Task { await model.newChat() } }
                    )
                    .frame(maxWidth: 520)
                }
            } else if model.isEmptyChat {
                AskEmptyChat(model: model)
            } else {
                turns
            }
        }
    }

    private var turns: some View {
        ScrollViewReader { proxy in
            ScrollView {
                // A plain VStack, not a lazy one: a chat holds at most 500 messages, and
                // the bottom anchor only lands exactly when every turn's height is known.
                VStack(alignment: .leading, spacing: 20) {
                    if model.trimmed > 0 {
                        let n = model.trimmed
                        Text("Ask does not show the \(Display.number(n)) earliest \(n == 1 ? "message" : "messages") in this chat.")
                            .font(.system(size: 12))
                            .foregroundStyle(Palette.muted)
                    }
                    ForEach(Array(model.turns.enumerated()), id: \.element.id) { index, turn in
                        if turn.role == .user {
                            AskQuestionBubble(text: turn.content)
                        } else {
                            AskAnswerView(
                                model: model, turn: turn,
                                next: index + 1 < model.turns.count ? model.turns[index + 1] : nil)
                        }
                    }
                    if let p = model.pending, model.busyHere {
                        AskPendingLine(pending: p)
                    }
                    Color.clear.frame(height: 1).id("bottom")
                }
                .padding(.horizontal, 28)
                .padding(.vertical, 20)
                .frame(maxWidth: 780)
                .frame(maxWidth: .infinity)
            }
            .defaultScrollAnchor(.bottom)
            .onChange(of: model.turns.count) { scrollDown(proxy) }
            .onChange(of: model.pending?.runID) { scrollDown(proxy) }
            .onAppear { scrollDown(proxy, animated: false) }
        }
    }

    private func scrollDown(_ proxy: ScrollViewProxy, animated: Bool = true) {
        DispatchQueue.main.async {
            if animated {
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo("bottom", anchor: .bottom) }
            } else {
                proxy.scrollTo("bottom", anchor: .bottom)
            }
        }
    }

    // MARK: Composer

    private var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .bottom, spacing: 8) {
                AskComposerField(
                    text: $model.draft,
                    placeholder: "Type a question about a person or a topic",
                    // Editable while answering, as in the web page: you can write the next
                    // question, and a failed one only comes back into an empty box (NC-082).
                    isEnabled: model.gone == nil,
                    focusRequest: model.composerFocus,
                    height: $composerHeight,
                    onSubmit: submit
                )
                .frame(height: composerHeight)
                .padding(.horizontal, 6)
                .padding(.vertical, 2)
                .background(
                    RoundedRectangle(cornerRadius: Metrics.radiusSmall)
                        .fill(model.gone != nil ? Palette.panel2 : Palette.panel))
                .overlay(RoundedRectangle(cornerRadius: Metrics.radiusSmall).strokeBorder(Palette.lineStrong))
                sendButton
            }
            HStack(spacing: 8) {
                AskShapeBar(model: model)
                Spacer()
                Text("Return sends · Shift-Return adds a line")
                    .font(.system(size: 10.5))
                    .foregroundStyle(Palette.faint)
            }
        }
        .padding(.horizontal, 16)
        .padding(.top, 10)
        .padding(.bottom, 12)
        .frame(maxWidth: 820)
        .frame(maxWidth: .infinity)
        .background(Palette.panel)
    }

    @ViewBuilder private var sendButton: some View {
        if model.busyHere {
            // ask.js:1453-1457: one button that swaps role; disabled once pressed.
            let stopping = model.pending?.stopping == true
            Button(stopping ? "Stopping…" : "Stop") { model.stop() }
                .controlSize(.large)
                .disabled(stopping)
                .keyboardShortcut(.cancelAction)
                .help("Stop this answer and keep what it has found")
        } else {
            Button("Ask", action: submit)
                .controlSize(.large)
                .buttonStyle(.borderedProminent)
                .tint(Palette.accent)
                .disabled(
                    model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || model.gone != nil
                        || model.busyElsewhere || model.transcriptState != .loaded)
                .help(model.busyElsewhere ? "Another chat is still answering. Wait for it to finish." : "Send (Return)")
        }
    }

    private func submit() {
        guard !model.busyElsewhere else { return }
        model.send(model.draft, fromComposer: true)
    }

    // MARK: Toast

    @ViewBuilder private var toast: some View {
        if let t = model.toast {
            Text(t)
                .font(.system(size: 12, weight: .medium))
                .foregroundStyle(Palette.onAction)
                .padding(.horizontal, 12)
                .padding(.vertical, 6)
                .background(Capsule().fill(Palette.action.opacity(0.92)))
                .padding(.bottom, 92 + composerHeight - AskComposerField.minHeight)
                .transition(.opacity)
                .accessibilityAddTraits(.updatesFrequently)
                .allowsHitTesting(false)
        }
    }
}

/// "Searching your network 12s" while this chat answers; "Stopping" once Stop was pressed.
/// The seconds come from the start time, never a counter (ask.js:743-760).
private struct AskPendingLine: View {
    let pending: AskModel.Pending

    var body: some View {
        HStack(spacing: 8) {
            ProgressView().controlSize(.small)
            Text(pending.stopping ? "Stopping" : "Searching your network")
                .foregroundStyle(Palette.muted)
            TimelineView(.periodic(from: pending.startedAt, by: 1)) { context in
                Text("\(max(0, Int(context.date.timeIntervalSince(pending.startedAt).rounded())))s")
                    .monospacedDigit()
                    .foregroundStyle(Palette.faint)
            }
        }
        .font(.system(size: 12))
        .accessibilityElement(children: .combine)
    }
}

/// A new or empty chat: the greeting, what Ask is for, and the intent chips that put a
/// phrase in the composer without sending it (ask.js:53-58, 1218-1232).
private struct AskEmptyChat: View {
    @AppEnvironment private var app
    let model: AskModel

    static let intents: [(label: String, prefill: String)] = [
        ("Find introductions", "Who could introduce me to "),
        ("Overdue contacts", "Who should I contact this month?"),
        ("Prepare a meeting", "What do I know about "),
        ("Find expertise", "Who in my network knows about "),
    ]

    var body: some View {
        VStack(spacing: 12) {
            Text(Display.greeting(selfName: model.selfName))
                .font(.system(size: 22, weight: .semibold))
                .foregroundStyle(Palette.ink)
            Text("Find who to invite, who to introduce, or who to contact. Every answer comes from your own contacts.")
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.muted)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
            HStack(spacing: 6) {
                ForEach(Self.intents, id: \.label) { intent in
                    Button(intent.label) {
                        model.draft = intent.prefill
                        model.composerFocus += 1
                    }
                    .controlSize(.small)
                }
            }
            .padding(.top, 4)
            if model.aiOff {
                AskNotice(content: .init(text: "Ask has no API key. Answers use keyword search.", offersSettings: true))
                    .frame(maxWidth: 520)
                    .padding(.top, 8)
            }
        }
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// A calm centred block for empty, error and missing states.
private struct AskCentered<Content: View>: View {
    @ViewBuilder let content: Content

    var body: some View {
        VStack(spacing: 6) { content }
            .font(.system(size: 12))
            .multilineTextAlignment(.center)
            .padding(20)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}
