import AppKit
import SwiftUI

// The three setup sheets RootView presents when `app.setupFlow` is set: Full Disk Access
// (FullDiskAccess*.swift, the web page's static/app/fda.js), Gmail (Gmail*.swift,
// static/app/gmail.js) and pasting a list of people (Paste*.swift, static/app/intake.js).
// Each closes by setting `app.setupFlow = nil`. The small parts all three share (the
// sheet's frame, a status pill, a one-line notice, text with bold words) live here.

struct SetupFlowView: View {
    let flow: SetupFlow

    var body: some View {
        switch flow {
        case .fullDiskAccess: FullDiskAccessSheet()
        case .gmail: GmailSheet()
        case .paste: PasteSheetRemembered()
        }
    }
}

// MARK: - The sheet's frame

/// Title and one plain sentence at the top, scrolling content, and a bar of buttons at the
/// foot (the place a Mac sheet keeps them). The foot always ends with Close (Esc). The
/// sheet grows with its content up to `size`, then the middle scrolls.
///
/// `notices` sit between the title and the scrolling part and never scroll: they are where
/// a button's answer goes ("Path copied.", an engine refusal), so the answer is on screen
/// even when the button that caused it was pressed at the bottom of a long sheet.
struct SetupSheetChrome<Header: View, Content: View, Footer: View>: View {
    let title: String
    let subtitle: String
    let size: CGSize
    let notices: [SetupNotice]
    @ViewBuilder let header: () -> Header
    @ViewBuilder let content: () -> Content
    @ViewBuilder let footer: () -> Footer
    @AppEnvironment private var app
    @Environment(\.dismiss) private var dismiss
    @Environment(\.setupSheetUnclipped) private var unclipped
    /// The content's own height, so the sheet can ask for exactly that much room.
    @State private var contentHeight: CGFloat = 0

    init(
        title: String, subtitle: String, size: CGSize, notices: [SetupNotice] = [],
        @ViewBuilder header: @escaping () -> Header = { EmptyView() },
        @ViewBuilder content: @escaping () -> Content,
        @ViewBuilder footer: @escaping () -> Footer = { EmptyView() }
    ) {
        self.title = title
        self.subtitle = subtitle
        self.size = size
        self.notices = notices
        self.header = header
        self.content = content
        self.footer = footer
    }

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .top, spacing: 12) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(title)
                        .font(.system(size: Metrics.fsTitle + 2, weight: .semibold))
                        .foregroundStyle(Palette.ink)
                    Text(subtitle)
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Spacer(minLength: 8)
                header()
            }
            .padding(.horizontal, Metrics.padView)
            .padding(.top, 18)
            .padding(.bottom, 14)
            Divider().overlay(Palette.line)
            if !notices.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(notices.enumerated()), id: \.offset) { _, n in SetupNoticeBanner(notice: n) }
                }
                .padding(.horizontal, Metrics.padView)
                .padding(.top, Metrics.gap)
                .transition(.opacity)
            }
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    content()
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(Metrics.padView)
                .onGeometryChange(for: CGFloat.self) { $0.size.height } action: { contentHeight = $0 }
            }
            .scrollContentBackground(.hidden)
            // A sheet takes its content's ideal size; a ScrollView on its own has none worth
            // asking for, so say how tall the content is (the outer frame still caps it).
            .frame(idealHeight: contentHeight > 0 ? contentHeight : nil)
            Divider().overlay(Palette.line)
            HStack(spacing: 8) {
                footer()
                Spacer(minLength: 8)
                Button("Close") { close() }
                    .keyboardShortcut(.cancelAction)
            }
            .controlSize(.regular)
            .padding(.horizontal, Metrics.padView)
            .frame(height: 52)
        }
        // As tall as the content needs, up to `size.height`; past that the middle scrolls.
        .frame(width: size.width)
        .frame(minHeight: 280, maxHeight: unclipped ? .infinity : size.height)
        .background(Palette.panel)
    }

    private func close() {
        app.setupFlow = nil
        dismiss()
    }
}

/// `--snapshot` only: lift the sheet's height cap so one tall picture shows everything
/// below the fold. Never set by the app itself. (A plain key, not `@Entry`: that macro's
/// plugin ships with full Xcode only.)
private struct SetupSheetUnclippedKey: EnvironmentKey {
    static let defaultValue = false
}

extension EnvironmentValues {
    var setupSheetUnclipped: Bool {
        get { self[SetupSheetUnclippedKey.self] }
        set { self[SetupSheetUnclippedKey.self] = newValue }
    }
}

// MARK: - Small shared parts

/// How a line or pill reads. Colour is state, as on the web page: a refusal is red,
/// something that needs you is gold, working is green, everything ordinary is neutral.
enum SetupTone: Sendable {
    case plain, ok, warn, bad, faint

    var color: Color {
        switch self {
        case .plain: Palette.muted
        case .ok: Palette.green
        case .warn: Palette.gold
        case .bad: Palette.red
        case .faint: Palette.faint
        }
    }

    var soft: Color {
        switch self {
        case .plain, .faint: Palette.hover
        case .ok: Palette.greenSoft
        case .warn: Palette.goldSoft
        case .bad: Palette.redSoft
        }
    }
}

/// A small pill: "New person", "In use", "Matches more than one".
struct SetupTag: View {
    let text: String
    var tone: SetupTone = .plain

    var body: some View {
        Text(text)
            .font(.system(size: Metrics.fsOverline, weight: .medium))
            .foregroundStyle(tone == .plain ? Palette.ink : tone.color)
            .padding(.horizontal, 7)
            .padding(.vertical, 2)
            .background(RoundedRectangle(cornerRadius: Metrics.radiusPill).fill(tone.soft))
            .fixedSize()
    }
}

/// One line with a small symbol: a refusal, a warning, a confirmation.
struct SetupLine: View {
    let text: String
    var tone: SetupTone = .plain
    var symbol: String? = "info.circle"

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            if let symbol {
                Image(systemName: symbol)
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(tone.color)
            }
            Text(text)
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(tone == .plain || tone == .faint ? Palette.muted : Palette.ink)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
        }
    }
}

/// The overline above a group: "What the paste held", "Links in your paste".
struct SetupSectionLabel: View {
    let text: String
    var body: some View {
        Text(text)
            .font(.system(size: Metrics.fsOverline, weight: .semibold))
            .foregroundStyle(Palette.muted)
    }
}

/// A short list of sentences, each a bullet.
struct SetupBullets: View {
    let lines: [String]
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            ForEach(Array(lines.enumerated()), id: \.offset) { _, line in
                HStack(alignment: .firstTextBaseline, spacing: 6) {
                    Text(verbatim: "•").foregroundStyle(Palette.faint)
                    Text(line)
                        .font(.system(size: Metrics.fsUI))
                        .foregroundStyle(Palette.ink)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
        }
    }
}

/// Text whose `**bold**` and `` `code` `` parts are drawn as such. Only for the app's own
/// wording: a person's name or address is never passed through it.
struct SetupRichText: View {
    let markdown: String
    var size: CGFloat = Metrics.fsUI
    var color: Color = Palette.ink

    var body: some View {
        Text(Self.attributed(markdown))
            .font(.system(size: size))
            .foregroundStyle(color)
            .fixedSize(horizontal: false, vertical: true)
    }

    static func attributed(_ s: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: s, options: options)) ?? AttributedString(s)
    }
}

/// What the web page shows as a toast, kept in the sheet as one line until the next
/// action replaces it, so it can be read at the pace of the person reading it.
struct SetupNotice: Equatable, Sendable {
    let text: String
    var tone: SetupTone = .plain
}

struct SetupNoticeBanner: View {
    let notice: SetupNotice

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Image(systemName: notice.tone == .ok ? "checkmark.circle.fill" : notice.tone == .bad ? "exclamationmark.circle.fill" : "info.circle.fill")
                .foregroundStyle(notice.tone == .plain ? Palette.accent : notice.tone.color)
            Text(notice.text)
                .font(.system(size: Metrics.fsUI))
                .foregroundStyle(Palette.ink)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            Spacer(minLength: 0)
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall)
            .fill(notice.tone == .plain ? Palette.accentSoft : notice.tone.soft))
        .accessibilityElement(children: .combine)
    }
}

/// A rounded panel with a hairline, the web page's card.
struct SetupCard<Content: View>: View {
    var tone: SetupTone? = nil
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 10) { content() }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(14)
            .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.panel2))
            .overlay(
                RoundedRectangle(cornerRadius: Metrics.radiusSmall)
                    .strokeBorder(tone.map { $0.color.opacity(0.55) } ?? Palette.line, lineWidth: 1))
    }
}

/// Monospaced program output, scrollable, selectable.
struct SetupOutputBox: View {
    let text: String
    var body: some View {
        ScrollView {
            Text(text)
                .font(.system(size: Metrics.fsOverline, design: .monospaced))
                .foregroundStyle(Palette.ink)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(10)
        }
        .frame(maxHeight: 180)
        .background(RoundedRectangle(cornerRadius: Metrics.radiusSmall).fill(Palette.bg))
    }
}

// MARK: - Words

enum SetupWords {
    /// An error as one sentence: the engine's own words (capitalised, ending in a full
    /// stop), or the client's plain description. Never a status code or a route
    /// (NC-363: EngineError keeps those for the log).
    static func sentence(_ error: Error) -> String {
        finished(EngineError.sentence(for: error))
    }

    static func finished(_ raw: String) -> String {
        var s = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = s.first else { return "Something went wrong. Try again." }
        s = first.uppercased() + s.dropFirst()
        if let last = s.last, !".!?…)”\"".contains(last) { s += "." }
        return s
    }

    /// "a", "a and b", "a, b and c" — no Oxford comma, matching the helper's own grammar.
    // from static/app/fda.js:59-63
    static func nameList(_ names: [String]) -> String {
        guard let last = names.last else { return "" }
        if names.count == 1 { return last }
        return names.dropLast().joined(separator: ", ") + " and " + last
    }

    /// "1 person", "3 people".
    // from static/app/intake.js:92
    static func people(_ n: Int) -> String { "\(n) \(n == 1 ? "person" : "people")" }

    /// Opens a page or a System Settings pane in its own app. Only ever called from a
    /// button the person pressed.
    @MainActor
    static func open(_ url: URL) {
        NSWorkspace.shared.open(url)
    }
}
