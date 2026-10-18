import AppKit
import SwiftUI

// One turn of a chat: the question on the right in a tinted bubble, the answer on the left
// with what it searched and cost above it, any notice, the text, the quiet lines about what
// it left out, the buttons when it asked something back, and the people it names.

struct AskQuestionBubble: View {
    let text: String

    var body: some View {
        HStack {
            Spacer(minLength: 80)
            Text(text)
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.ink)
                .lineSpacing(2)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .background(RoundedRectangle(cornerRadius: 14, style: .continuous).fill(Palette.accentSoft))
        }
    }
}

struct AskAnswerView: View {
    @AppEnvironment private var app
    let model: AskModel
    let turn: AskTurn
    /// The turn after this one, for "is this question answered" (ask.js:1259-1268).
    let next: AskTurn?

    var body: some View {
        let (text, notice) = AskReply.normalize(turn.content)
        let cards = turn.contacts
        let shown =
            !text.isEmpty
            ? text
            : (cards.isEmpty && notice == nil && turn.failure == nil && turn.withheld == 0
                ? "No contact matches that question." : "")
        VStack(alignment: .leading, spacing: 8) {
            AskProvenance(searched: turn.searched, trail: turn.trail, spend: turn.spend)
            if let f = turn.failure { failureNotice(f) }
            if let notice { AskNotice(content: notice) }
            if !shown.isEmpty { AskMarkdownView(text: shown) }
            quietLines(cards: cards)
            if let q = turn.question, !q.options.isEmpty {
                let answered = next?.role == .user
                AskOptionsView(
                    question: q, answered: answered, picked: answered ? next?.content ?? "" : "",
                    busy: model.pending != nil
                ) { model.send($0, fromComposer: false) }
            }
            if !cards.isEmpty { AskPeopleList(model: model, turnID: turn.id, cards: cards) }
            if !shown.isEmpty || !cards.isEmpty { actions(shown: shown, hasCards: !cards.isEmpty) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder private func failureNotice(_ f: AskFailure) -> some View {
        if f.timedOut {
            AskNotice(
                content: .init(
                    tone: .bad,
                    text: f.stoppedByYou
                        ? "You stopped the answer. Reopen this chat to see how far it got."
                        : "Ask stopped waiting after 3 minutes. The answer can still be running on this Mac. Reopen this chat in a moment to see if it finished.",
                    detail: f.message),
                actionTitle: f.stoppedByYou ? nil : "Stop the answer",
                action: { model.stopTimedOut(turnID: turn.id) })
        } else {
            // The request failed and nothing answered at all (NC-347): say what went wrong, in
            // the engine's own sentence, and offer to send it again.
            AskNotice(
                content: .init(tone: .bad, text: f.message, detail: f.detail),
                actionTitle: model.pending == nil ? "Retry" : nil,
                action: { model.retry(turnID: turn.id) })
        }
    }

    /// What the answer left out, said once and quietly (ask.js:1315-1355).
    @ViewBuilder private func quietLines(cards: [AskCard]) -> some View {
        if turn.truncated {
            quiet("The answer stopped early." + (cards.isEmpty ? "" : " The list below is incomplete."))
        }
        if let m = turn.cardsMatched, m > cards.count {
            quiet("The answer lists \(cards.count). The searches found \(m).")
        }
        if let t = turn.totalMatched, t > cards.count {
            quiet("Keyword search matched \(t) people. The \(cards.count) closest are below.")
        }
        if let c = turn.context, c.fedToLLM, c.trimmed > 0 {
            quiet("Ask read the last \(c.turns) messages of this chat. It did not read \(c.trimmed) earlier messages.")
        }
        if turn.withheld > 0 {
            quiet(
                turn.withheld == 1
                    ? "One person in this answer is hidden now, so their card is not shown."
                    : "\(turn.withheld) people in this answer are hidden now, so their cards are not shown.")
        }
    }

    private func quiet(_ s: String) -> some View {
        Text(s)
            .font(.system(size: 12))
            .foregroundStyle(Palette.muted)
            .fixedSize(horizontal: false, vertical: true)
    }

    private func actions(shown: String, hasCards: Bool) -> some View {
        HStack(spacing: 14) {
            if !shown.isEmpty {
                Button("Copy Answer") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(shown, forType: .string)
                    model.showToast("Copied")
                }
            }
            if hasCards {
                Button("Open People") { app.section = .people }
            }
        }
        .buttonStyle(.link)
        .font(.system(size: Metrics.fsOverline))
        .foregroundStyle(Palette.muted)
        .padding(.top, 2)
    }
}

// MARK: - Provenance

/// "Searched N contacts · M conversations · K messages · T tool calls · $cost", expandable
/// to the tool trail and the bill. Exact web wording (ask.js:398-479).
struct AskProvenance: View {
    let searched: Searched?
    let trail: [TrailEntry]
    let spend: Spend?
    @State private var open = false

    var body: some View {
        if searched != nil || spend != nil || !trail.isEmpty {
            let n = trail.count
            if n == 0 && spend == nil {
                HStack(spacing: 8) {
                    Text(counts)
                    keywordTag
                }
                .font(.system(size: Metrics.fsOverline))
                .foregroundStyle(Palette.muted)
            } else {
                VStack(alignment: .leading, spacing: 3) {
                    Button {
                        withAnimation(.easeOut(duration: 0.15)) { open.toggle() }
                    } label: {
                        HStack(spacing: 4) {
                            Text(collapsedLine)
                            Image(systemName: "chevron.right")
                                .font(.system(size: 9, weight: .semibold))
                                .rotationEffect(.degrees(open ? 90 : 0))
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .help(open ? "Hide the searches" : "Show the searches and what they cost")
                    if open {
                        ForEach(Array(trail.enumerated()), id: \.offset) { _, t in
                            Text(row(t)).textSelection(.enabled)
                        }
                        if let spend { Text(bill(spend)).textSelection(.enabled) }
                    }
                }
                .font(.system(size: Metrics.fsOverline))
                .foregroundStyle(Palette.muted)
            }
        }
    }

    private var counts: String {
        guard let s = searched else { return "This answer" }
        var line = "Searched \(Display.number(s.contacts)) contacts · \(Display.number(s.conversations)) conversations"
        if s.messages > 0 { line += " · \(Display.number(s.messages)) messages" }
        return line
    }

    private var collapsedLine: String {
        var line = counts
        let n = trail.count
        if n > 0 { line += " · \(n) tool call\(n == 1 ? "" : "s")" }
        if let spend { line += " · " + Display.cost(usd: spend.usd) }
        return line
    }

    private var keywordTag: some View {
        Text("Keyword search")
            .font(.system(size: 10.5, weight: .medium))
            .foregroundStyle(Palette.gold)
            .padding(.horizontal, 6)
            .padding(.vertical, 1.5)
            .background(Capsule().fill(Palette.goldSoft))
    }

    private func row(_ t: TrailEntry) -> String {
        let tail: String
        if let label = t.label, !label.isEmpty {
            tail = label
        } else if t.total > t.matched {
            tail = "\(t.matched) of \(Display.number(t.total)) matched"
        } else {
            tail = "\(t.matched) matched"
        }
        return "\(t.tool) · \(t.detail) · \(tail)"
    }

    private func bill(_ s: Spend) -> String {
        var parts: [String] = [String(format: "$%.4f", s.usd)]
        // Older saved answers carry no prices; they show none rather than today's.
        if s.priceInPerMtok > 0 || s.priceOutPerMtok > 0 {
            parts.append(
                "priced at $\(Self.jsNumber(s.priceInPerMtok)) per million tokens in, $\(Self.jsNumber(s.priceOutPerMtok)) out")
        }
        var tail = "\(s.rounds) round\(s.rounds == 1 ? "" : "s") · \(s.model) · \(Display.number(s.inputTokens)) tokens in · \(Display.number(s.outputTokens)) out"
        if s.cacheReadTokens > 0 { tail += " · \(Display.number(s.cacheReadTokens)) from cache" }
        parts.append(tail)
        return parts.joined(separator: " · ")
    }

    /// A number the way JavaScript prints it: 3 → "3", 0.8 → "0.8".
    static func jsNumber(_ d: Double) -> String {
        d == d.rounded() && abs(d) < 1e15 ? String(Int(d)) : String(d)
    }
}

// MARK: - Notice

/// One diagnosis, at most one action, and the raw cause on request (ask.js:189-205).
struct AskNotice: View {
    @AppEnvironment private var app
    let content: AskNoticeContent
    var actionTitle: String? = nil
    var action: (() -> Void)? = nil
    @State private var showDetail = false

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Image(systemName: content.tone == .bad ? "exclamationmark.circle" : "info.circle")
                    .foregroundStyle(content.tone == .bad ? Palette.danger : Palette.gold)
                Text(content.text)
                    .foregroundStyle(Palette.ink)
                    .fixedSize(horizontal: false, vertical: true)
                    .frame(maxWidth: .infinity, alignment: .leading)
                if let actionTitle, let action {
                    Button(actionTitle, action: action).controlSize(.small)
                }
                if content.offersSettings {
                    // ask.js:1238, 1282: go("settings?row=ai"): the Settings page, opened on
                    // the row holding the API key.
                    Button("Open Settings") { app.openSettings(row: "ai") }
                        .controlSize(.small)
                        .help("The API key and the anthropic package are set in Settings.")
                }
                if !content.detail.isEmpty {
                    Button(showDetail ? "Hide detail" : "Show detail") { showDetail.toggle() }
                        .buttonStyle(.link)
                        .font(.system(size: Metrics.fsOverline))
                }
            }
            if showDetail && !content.detail.isEmpty {
                Text(content.detail)
                    .font(.system(size: Metrics.fsOverline, design: .monospaced))
                    .foregroundStyle(Palette.muted)
                    .textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
                    .padding(.leading, 22)
            }
        }
        .font(.system(size: 12))
        .padding(.horizontal, 10)
        .padding(.vertical, 8)
        .background(
            RoundedRectangle(cornerRadius: Metrics.radiusSmall)
                .fill(content.tone == .bad ? Palette.redSoft.opacity(0.6) : Palette.panel2))
        .overlay(
            RoundedRectangle(cornerRadius: Metrics.radiusSmall)
                .strokeBorder(content.tone == .bad ? Palette.redSoft : Palette.line))
    }
}

// MARK: - Asked back

/// The answer asked a question: one button per option. A single choice sends on click; a
/// multiple choice sends the picks joined by ", ". Once answered they stay, inert, with the
/// chosen ones marked (ask.js:349-390).
struct AskOptionsView: View {
    let question: AskQuestion
    let answered: Bool
    let picked: String
    let busy: Bool
    let onPick: (String) -> Void
    @State private var selected: Set<String> = []

    var body: some View {
        AskFlowLayout(spacing: 6) {
            ForEach(Array(question.options.enumerated()), id: \.offset) { _, option in
                if answered {
                    chip(option, on: chose(option))
                } else {
                    Button {
                        if question.multi {
                            if selected.contains(option) { selected.remove(option) } else { selected.insert(option) }
                        } else {
                            onPick(option)
                        }
                    } label: {
                        chip(option, on: question.multi && selected.contains(option))
                    }
                    .buttonStyle(.plain)
                    .disabled(busy)
                }
            }
            if question.multi && !answered {
                Button("Send") { onPick(question.options.filter { selected.contains($0) }.joined(separator: ", ")) }
                    .controlSize(.small)
                    .buttonStyle(.borderedProminent)
                    .disabled(busy || selected.isEmpty)
            }
        }
        .opacity(answered ? 0.85 : 1)
    }

    private func chose(_ o: String) -> Bool {
        func norm(_ t: String) -> String { t.trimmingCharacters(in: .whitespacesAndNewlines).lowercased() }
        let whole = norm(picked)
        return whole == norm(o) || whole.split(separator: ",").map { norm(String($0)) }.contains(norm(o))
    }

    private func chip(_ text: String, on: Bool) -> some View {
        Text(text)
            .font(.system(size: 12, weight: on ? .semibold : .regular))
            .foregroundStyle(on ? Palette.accentInk : Palette.ink)
            .padding(.horizontal, 10)
            .padding(.vertical, 4)
            .background(Capsule().fill(on ? Palette.accentSoft : Palette.panel2))
            .overlay(Capsule().strokeBorder(on ? Palette.accent.opacity(0.5) : Palette.lineStrong))
            .contentShape(Capsule())
    }
}

/// Lays chips out left to right, wrapping onto new lines.
struct AskFlowLayout: Layout {
    var spacing: CGFloat = 6

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        let width = proposal.width ?? .infinity
        var x: CGFloat = 0, y: CGFloat = 0, row: CGFloat = 0, widest: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(.unspecified)
            if x > 0 && x + size.width > width {
                y += row + spacing
                x = 0
                row = 0
            }
            x += size.width + spacing
            row = max(row, size.height)
            widest = max(widest, x - spacing)
        }
        return CGSize(width: proposal.width ?? widest, height: y + row)
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        var x = bounds.minX, y = bounds.minY, row: CGFloat = 0
        for s in subviews {
            let size = s.sizeThatFits(.unspecified)
            if x > bounds.minX && x + size.width > bounds.maxX {
                y += row + spacing
                x = bounds.minX
                row = 0
            }
            s.place(at: CGPoint(x: x, y: y), proposal: ProposedViewSize(size))
            x += size.width + spacing
            row = max(row, size.height)
        }
    }
}
