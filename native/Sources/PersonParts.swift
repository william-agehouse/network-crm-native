import AppKit
import SwiftUI

// Small pieces the person page is built from: the avatar with its closeness ring, the
// click-to-edit text field, section headings, tags, the one ink button, and the copy helper.

// MARK: - Avatar

/// A round photo, or initials on the name's own colour, inside the closeness ring
/// (static/app/api.js:199-252, static/css/ui.css:426-441).
struct PersonAvatar: View {
    let name: String
    let photo: String
    let strength: Int
    let tier: String
    let size: CGFloat
    var ring = true

    @AppEnvironment private var app
    @State private var image: NSImage?

    private var ringWidth: CGFloat { max(2, size * 0.04) }
    /// The web page's 2.5 px gap between ring and picture, scaled.
    private var inner: CGFloat { ring ? size - 2 * (ringWidth + max(2, size * 0.035)) : size }

    var body: some View {
        ZStack {
            if ring {
                Circle().stroke(Palette.line, lineWidth: ringWidth)
                Circle()
                    .trim(from: 0, to: Display.ringFraction(strength: strength))
                    .stroke(Display.Tier(label: tier).color, style: StrokeStyle(lineWidth: ringWidth, lineCap: .butt))
                    .rotationEffect(.degrees(-90))
            }
            picture
                .frame(width: inner, height: inner)
                .clipShape(Circle())
        }
        .padding(ring ? ringWidth / 2 : 0)
        .frame(width: size, height: size)
        .task(id: photo) { image = await PersonPhotoCache.image(for: photo, client: app.client) }
        .help(Display.ringTooltip(strength: strength, label: tier))
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(Text(verbatim: name))
    }

    @ViewBuilder private var picture: some View {
        if let image {
            Image(nsImage: image).resizable().scaledToFill()
        } else {
            let colors = Display.avatarColors(name)
            ZStack {
                LinearGradient(colors: [colors.top, colors.bottom], startPoint: .topLeading, endPoint: .bottomTrailing)
                Text(verbatim: Display.initials(name))
                    .font(.system(size: inner * 0.38, weight: .semibold))
                    .foregroundStyle(colors.ink)
                    .lineLimit(1)
                    .minimumScaleFactor(0.5)
            }
        }
    }
}

// MARK: - Click-to-edit text

/// One editable value. It reads as plain text until clicked; Return (or leaving the field)
/// saves when the text changed, Escape puts the old value back, and a short "Saved" shows
/// beside it. A failed save keeps what was typed and says why underneath
/// (people.js:148-221, PropRow).
struct PersonEditableText: View {
    let label: String
    let value: String
    var ghost: String? = nil
    var multiline = false
    var font: Font = .system(size: Metrics.fsUI)
    var color: Color = Palette.ink
    /// false for the name: emptying it puts the old name back instead of saving.
    var allowEmpty = true
    let onSave: @MainActor (String) async throws -> Void

    @State private var draft = ""
    @State private var phase: Phase = .idle
    @State private var hovering = false
    @State private var sending: String?
    @FocusState private var focused: Bool

    enum Phase: Equatable { case idle, saving, saved, failed(String) }

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(alignment: .firstTextBaseline, spacing: 6) {
                TextField(
                    label, text: $draft, prompt: Text(verbatim: ghost ?? label).foregroundStyle(Palette.faint),
                    axis: multiline ? .vertical : .horizontal
                )
                .textFieldStyle(.plain)
                .font(font)
                .foregroundStyle(color)
                .lineLimit(multiline ? 1...8 : 1...1)
                .fixedSize(horizontal: false, vertical: multiline)
                .focused($focused)
                .onSubmit { commit() }
                .onExitCommand {
                    draft = value
                    phase = .idle
                    focused = false
                }
                .padding(.horizontal, 4)
                .padding(.vertical, 2)
                .background(
                    RoundedRectangle(cornerRadius: 5)
                        .fill(focused ? Palette.panel : hovering ? Palette.hover : Color.clear))
                .overlay(
                    RoundedRectangle(cornerRadius: 5)
                        .strokeBorder(focused ? Palette.accent : Color.clear, lineWidth: 1))
                .padding(.horizontal, -4)
                .onHover { hovering = $0 }
                .help("Edit \(label.lowercased())")
                .accessibilityLabel(Text(verbatim: label))

                switch phase {
                case .saving:
                    ProgressView().controlSize(.mini)
                case .saved:
                    Text("Saved")
                        .font(.system(size: Metrics.fsOverline))
                        .foregroundStyle(Palette.green)
                        .transition(.opacity)
                default:
                    EmptyView()
                }
            }
            if case .failed(let message) = phase {
                Text(verbatim: message)
                    .font(.system(size: Metrics.fsOverline))
                    .foregroundStyle(Palette.danger)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .onAppear { draft = value }
        .onChange(of: value) { _, new in
            if !focused { draft = new }
        }
        .onChange(of: focused) { _, isFocused in
            if !isFocused { commit() }
        }
    }

    private func commit() {
        let next = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        // Saving needs a real change; Return then leaving the field must not send twice.
        guard next != value, next != sending else { return }
        if next.isEmpty && !allowEmpty {
            draft = value
            return
        }
        sending = next
        phase = .saving
        Task {
            do {
                try await onSave(next)
                sending = nil
                withAnimation(.easeOut(duration: 0.15)) { phase = .saved }
                try? await Task.sleep(for: .seconds(2))
                if phase == .saved { withAnimation(.easeOut(duration: 0.3)) { phase = .idle } }
            } catch {
                sending = nil
                phase = .failed(PersonStore.say(error))
            }
        }
    }
}

// MARK: - Sections and tags

/// A heading with an optional caption and trailing control, then the content.
struct PersonSection<Content: View, Accessory: View>: View {
    let title: String
    var caption: String? = nil
    @ViewBuilder var accessory: Accessory
    @ViewBuilder var content: Content

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Text(title)
                    .font(.system(size: Metrics.fsUI, weight: .semibold))
                    .foregroundStyle(Palette.ink)
                if let caption {
                    PersonTag(text: caption)
                }
                Spacer(minLength: 0)
                accessory
            }
            content
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

extension PersonSection where Accessory == EmptyView {
    init(title: String, caption: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.caption = caption
        self.accessory = EmptyView()
        self.content = content()
    }
}

/// A small neutral pill: a source, a marker, a topic.
struct PersonTag: View {
    let text: String
    var color: Color = Palette.muted
    var dot: Color? = nil

    var body: some View {
        HStack(spacing: 4) {
            if let dot { Circle().fill(dot).frame(width: 6, height: 6) }
            Text(verbatim: text).lineLimit(1)
        }
        .font(.system(size: Metrics.fsOverline, weight: .medium))
        .foregroundStyle(color)
        .padding(.horizontal, 6)
        .padding(.vertical, 2)
        .background(Capsule().fill(Palette.panel2))
        .overlay(Capsule().strokeBorder(Palette.line, lineWidth: 1))
    }
}

/// One plain sentence where content will appear.
struct PersonEmptyLine: View {
    let text: String
    var body: some View {
        Text(verbatim: text)
            .font(.system(size: Metrics.fsUI))
            .foregroundStyle(Palette.muted)
            .fixedSize(horizontal: false, vertical: true)
    }
}

/// The record's one filled button: actions are ink, not colour (DESIGN_DIRECTION §6).
struct PersonInkButtonStyle: ButtonStyle {
    @Environment(\.isEnabled) private var isEnabled
    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: Metrics.fsUI, weight: .medium))
            .foregroundStyle(Palette.onAction)
            .padding(.horizontal, 10)
            .frame(height: 24)
            .background(
                RoundedRectangle(cornerRadius: 6)
                    .fill(configuration.isPressed ? Palette.actionHover : Palette.action))
            .opacity(isEnabled ? 1 : 0.45)
    }
}

/// The confirmation at the foot of the page.
struct PersonFlash: View {
    let text: String
    var body: some View {
        Text(verbatim: text)
            .font(.system(size: Metrics.fsUI))
            .foregroundStyle(Palette.onAction)
            .lineLimit(2)
            .multilineTextAlignment(.center)
            .padding(.horizontal, 14)
            .padding(.vertical, 7)
            .background(Capsule().fill(Palette.action))
            .shadow(color: .black.opacity(0.15), radius: 6, y: 2)
            .padding(.bottom, 14)
            .padding(.horizontal, 16)
            .accessibilityAddTraits(.updatesFrequently)
    }
}

enum PersonClipboard {
    @MainActor
    static func copy(_ value: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(value, forType: .string)
    }
}

enum PersonWords {
    /// "1 message" / "3 messages", with thousands separators.
    static func count(_ n: Int, _ one: String, _ many: String? = nil) -> String {
        "\(Display.number(n)) \(n == 1 ? one : many ?? one + "s")"
    }
}
