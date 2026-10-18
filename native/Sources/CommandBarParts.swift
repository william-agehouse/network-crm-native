import AppKit
import SwiftUI

// Small pieces the command bar and first-run setup share: a round avatar (photo or
// initials, with the closeness ring when asked), the search field that hands arrow keys,
// Return and Escape to the bar, a neutral tag, and the web page's markdown-lite.
// Kept in these files rather than borrowed from People's, so one screen's rework never
// breaks the other's build.

// MARK: - Avatar

/// The web page's Avatar (`ui.js`, `ui.css:426-441`): the photo when there is one, else
/// the initials on the hashed two-colour gradient; `ring` adds the closeness ring.
struct CmdAvatar: View {
    let name: String
    let photo: String
    var size: CGFloat = 24
    /// Draw the closeness ring (`api.js:247-252`): fill = clamp(strength, 6, 100) percent.
    var ring = false
    var strength = 0
    var label = ""
    let client: EngineClient

    @State private var image: NSImage?

    var body: some View {
        let ringWidth: CGFloat = ring ? max(2, size * 0.04) : 0
        let inner = ring ? size - 2 * (ringWidth + 2.5) : size
        ZStack {
            if ring {
                Circle().stroke(Palette.line, lineWidth: ringWidth)
                Circle()
                    .trim(from: 0, to: Display.ringFraction(strength: strength))
                    .stroke(Display.Tier(label: label).color, style: StrokeStyle(lineWidth: ringWidth, lineCap: .round))
                    .rotationEffect(.degrees(-90))
            }
            face.frame(width: inner, height: inner).clipShape(Circle())
        }
        .padding(ringWidth / 2)
        .frame(width: size, height: size)
        .help(ring ? Display.ringTooltip(strength: strength, label: label) : "")
        .accessibilityHidden(true)
        .task(id: photo) {
            if let hit = CmdPhotoCache.images.object(forKey: photo as NSString) {
                image = hit
                return
            }
            image = nil
            guard !photo.isEmpty, let data = try? await client.photoData(photo), let loaded = NSImage(data: data)
            else { return }
            CmdPhotoCache.images.setObject(loaded, forKey: photo as NSString)
            if !Task.isCancelled { image = loaded }
        }
    }

    @ViewBuilder private var face: some View {
        if let image {
            Image(nsImage: image).resizable().interpolation(.high).aspectRatio(contentMode: .fill)
        } else {
            let c = Display.avatarColors(name)
            let side = ring ? size - 2 * (max(2, size * 0.04) + 2.5) : size
            ZStack {
                LinearGradient(colors: [c.top, c.bottom], startPoint: .topLeading, endPoint: .bottomTrailing)
                Text(Display.initials(name))
                    .font(.system(size: side * 0.38, weight: .semibold))
                    .foregroundStyle(c.ink)
                    .lineLimit(1)
                    .minimumScaleFactor(0.6)
            }
        }
    }
}

/// Photos already fetched, by their full relative path (which carries the photo's mtime,
/// so a replaced photo is a new key): typing in ⌘K redraws the same people many times.
@MainActor
enum CmdPhotoCache {
    static let images: NSCache<NSString, NSImage> = {
        let c = NSCache<NSString, NSImage>()
        c.countLimit = 200
        return c
    }()
}

// MARK: - Glyph

/// The fixed-width icon column of a row, so names line up whatever the icon (`ui.css:235`).
struct CmdGlyph: View {
    let symbol: String
    var size: CGFloat = 24

    var body: some View {
        Image(systemName: symbol)
            .font(.system(size: 13, weight: .regular))
            .foregroundStyle(Palette.muted)
            .frame(width: size, height: size)
            .background(RoundedRectangle(cornerRadius: 6).fill(Palette.panel2))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Palette.line))
            .accessibilityHidden(true)
    }
}

// MARK: - Tag

/// A small neutral pill: a place, a role, a topic (`ui.js:245-260`).
struct CmdTag: View {
    let text: String
    var help: String = ""

    var body: some View {
        Text(verbatim: text)
            .lineLimit(1)
            .font(.system(size: Metrics.fsOverline, weight: .medium))
            .foregroundStyle(Palette.muted)
            .padding(.horizontal, 6)
            .padding(.vertical, 2)
            .background(Capsule().fill(Palette.panel2))
            .overlay(Capsule().strokeBorder(Palette.line, lineWidth: 1))
            .help(help)
    }

    /// Location, role type, then topics up to three tags in all, then "+N" with the rest
    /// as its tooltip (`ui.js:245-260`, max 3).
    static func tags(metro: String, city: String, role: String, topics: [String], max: Int = 3) -> [(String, String)] {
        var out: [(String, String)] = []
        let loc = metro.isEmpty ? city : metro
        if !loc.isEmpty { out.append((loc, "")) }
        if !role.isEmpty { out.append((role, "")) }
        let room = Swift.max(0, max - out.count)
        out += topics.prefix(room).map { ($0, "") }
        let rest = topics.count - Swift.min(room, topics.count)
        if rest > 0 { out.append(("+\(rest)", topics.dropFirst(room).joined(separator: ", "))) }
        return out
    }
}

// MARK: - Search field

/// A borderless text field that gives ↑ ↓, Return and Escape to the command bar instead of
/// moving the caret, as the omnibox does (`main.js:486-491`). SwiftUI's TextField keeps
/// the arrow keys for itself on the Mac, so this is an AppKit field.
struct CmdSearchField: NSViewRepresentable {
    @Binding var text: String
    var placeholder: String
    /// Bump to put the caret in the field with everything selected.
    var focusRequest: Int
    var onMove: (Int) -> Void
    var onSubmit: () -> Void
    var onCancel: () -> Void

    func makeNSView(context: Context) -> NSTextField {
        let field = NSTextField()
        field.isBordered = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.bezelStyle = .roundedBezel
        field.font = .systemFont(ofSize: 17)
        field.placeholderString = placeholder
        field.stringValue = text
        field.lineBreakMode = .byTruncatingTail
        field.cell?.isScrollable = true
        field.cell?.wraps = false
        field.delegate = context.coordinator
        field.setAccessibilityLabel("Search people or run a command")
        DispatchQueue.main.async { field.window?.makeFirstResponder(field) }
        return field
    }

    func updateNSView(_ field: NSTextField, context: Context) {
        context.coordinator.parent = self
        if field.stringValue != text { field.stringValue = text }
        if context.coordinator.lastFocusRequest != focusRequest {
            context.coordinator.lastFocusRequest = focusRequest
            DispatchQueue.main.async {
                field.window?.makeFirstResponder(field)
                field.currentEditor()?.selectAll(nil)
            }
        }
    }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    @MainActor
    final class Coordinator: NSObject, NSTextFieldDelegate {
        var parent: CmdSearchField
        var lastFocusRequest: Int

        init(_ parent: CmdSearchField) {
            self.parent = parent
            self.lastFocusRequest = parent.focusRequest
        }

        func controlTextDidChange(_ note: Notification) {
            guard let field = note.object as? NSTextField else { return }
            parent.text = field.stringValue
        }

        func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
            switch selector {
            case #selector(NSResponder.moveUp(_:)): parent.onMove(-1); return true
            case #selector(NSResponder.moveDown(_:)): parent.onMove(1); return true
            case #selector(NSResponder.insertNewline(_:)): parent.onSubmit(); return true
            case #selector(NSResponder.cancelOperation(_:)): parent.onCancel(); return true
            default: return false
            }
        }
    }
}

// MARK: - Markdown-lite

/// The web page's `Md` (`ui.js:264-298`): paragraphs split on blank lines, `-`/`*`/`•`
/// bullet blocks, numbered steps kept as written, and **bold**, *italic*, `code` and bare
/// links inside a line.
struct CmdMarkdown: View {
    let text: String
    var size: CGFloat = Metrics.fsUI
    var color: Color = Palette.muted

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(Self.blocks(text).enumerated()), id: \.offset) { _, block in
                if block.bullets {
                    VStack(alignment: .leading, spacing: 3) {
                        ForEach(Array(block.lines.enumerated()), id: \.offset) { _, line in
                            HStack(alignment: .firstTextBaseline, spacing: 6) {
                                Text(verbatim: "•")
                                Text(Self.inline(line))
                            }
                        }
                    }
                } else {
                    Text(Self.inline(block.lines.joined(separator: "\n")))
                }
            }
        }
        .font(.system(size: size))
        .foregroundStyle(color)
        .lineSpacing(2)
        .fixedSize(horizontal: false, vertical: true)
        .textSelection(.enabled)
    }

    struct Block {
        let lines: [String]
        let bullets: Bool
    }

    static func blocks(_ text: String) -> [Block] {
        text.components(separatedBy: "\n\n").compactMap { raw in
            let lines = raw.components(separatedBy: "\n")
            let filled = lines.filter { !$0.trimmingCharacters(in: .whitespaces).isEmpty }
            guard !filled.isEmpty else { return nil }
            let bullet = /^\s*[-•*]\s+/
            if filled.allSatisfy({ $0.firstMatch(of: bullet) != nil }) {
                return Block(lines: filled.map { $0.replacing(bullet, with: "") }, bullets: true)
            }
            return Block(lines: lines, bullets: false)
        }
    }

    static func inline(_ line: String) -> AttributedString {
        let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
        return (try? AttributedString(markdown: line, options: options)) ?? AttributedString(line)
    }
}

// MARK: - Toast

/// One line at the foot of a sheet for a few seconds, like the web page's toast
/// (2.6 s, announced to VoiceOver).
struct CmdToast: View {
    let text: String

    var body: some View {
        Text(verbatim: text)
            .font(.system(size: Metrics.fsUI))
            .foregroundStyle(Palette.onAction)
            .multilineTextAlignment(.center)
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background(Capsule().fill(Palette.action))
            .shadow(color: .black.opacity(0.15), radius: 8, y: 2)
            .frame(maxWidth: 460)
            .onAppear { NSAccessibility.post(element: NSApp as Any, notification: .announcementRequested,
                                             userInfo: [.announcement: text, .priority: NSAccessibilityPriorityLevel.high.rawValue]) }
    }
}
