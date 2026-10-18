import AppKit
import SwiftUI

// The composer: a multi-line field where Return sends and Shift-Return (or Option-Return)
// starts a new line, as in the web page (ask.js:1426-1440). It is an NSTextView because a
// SwiftUI field cannot tell Return from Shift-Return, nor see when an input method (Japanese,
// Chinese) is still composing and Return only commits the candidate.

struct AskComposerField: NSViewRepresentable {
    @Binding var text: String
    var placeholder: String
    var isEnabled: Bool
    /// Bumped by the model to put the caret here.
    var focusRequest: Int
    /// Grows with what is typed, up to 8 lines, then scrolls (ask.js:321).
    @Binding var height: CGFloat
    var onSubmit: () -> Void

    static let font = NSFont.systemFont(ofSize: Metrics.fsUI)
    static let inset = NSSize(width: 2, height: 6)
    static let maxLines: CGFloat = 8

    static var lineHeight: CGFloat {
        let lm = NSLayoutManager()
        return lm.defaultLineHeight(for: font)
    }
    static var minHeight: CGFloat { lineHeight + inset.height * 2 }
    static var maxHeight: CGFloat { lineHeight * maxLines + inset.height * 2 }

    func makeCoordinator() -> Coordinator { Coordinator(self) }

    func makeNSView(context: Context) -> NSScrollView {
        let scroll = NSScrollView()
        scroll.drawsBackground = false
        scroll.borderType = .noBorder
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.hasHorizontalScroller = false

        let tv = ComposerTextView(frame: .zero)
        tv.delegate = context.coordinator
        tv.isRichText = false
        tv.importsGraphics = false
        tv.allowsUndo = true
        tv.font = Self.font
        tv.textColor = .labelColor
        tv.insertionPointColor = .labelColor
        tv.drawsBackground = false
        tv.textContainerInset = Self.inset
        tv.isVerticallyResizable = true
        tv.isHorizontallyResizable = false
        tv.autoresizingMask = [.width]
        tv.textContainer?.widthTracksTextView = true
        tv.textContainer?.lineFragmentPadding = 4
        tv.isAutomaticQuoteSubstitutionEnabled = false
        tv.isAutomaticDashSubstitutionEnabled = false
        tv.placeholder = placeholder
        tv.string = text
        tv.setAccessibilityLabel("Your question")
        tv.onSubmit = { context.coordinator.parent.onSubmit() }
        scroll.documentView = tv
        context.coordinator.textView = tv
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        let c = context.coordinator
        c.parent = self
        guard let tv = c.textView else { return }
        if tv.string != text {
            // Only text set from outside lands here (an intent chip, a staged draft, a
            // failed question put back): the caret goes after it, as ask.js:1150 does.
            tv.string = text
            tv.setSelectedRange(NSRange(location: (text as NSString).length, length: 0))
            tv.needsDisplay = true
            c.measure()
        }
        tv.isEditable = isEnabled
        tv.isSelectable = true
        tv.textColor = isEnabled ? .labelColor : .secondaryLabelColor
        tv.placeholder = placeholder
        tv.onSubmit = { c.parent.onSubmit() }
        if c.lastFocus != focusRequest {
            c.lastFocus = focusRequest
            DispatchQueue.main.async {
                if let w = tv.window, tv.isEditable { w.makeFirstResponder(tv) }
            }
        }
        DispatchQueue.main.async { c.measure() }
    }

    @MainActor
    final class Coordinator: NSObject, NSTextViewDelegate {
        var parent: AskComposerField
        weak var textView: ComposerTextView?
        var lastFocus: Int

        init(_ parent: AskComposerField) {
            self.parent = parent
            self.lastFocus = parent.focusRequest
        }

        func textDidChange(_ notification: Notification) {
            guard let tv = textView else { return }
            parent.text = tv.string
            tv.needsDisplay = true
            measure()
        }

        func measure() {
            guard let tv = textView, let lm = tv.layoutManager, let tc = tv.textContainer else { return }
            lm.ensureLayout(for: tc)
            let used = lm.usedRect(for: tc).height
            let h = min(AskComposerField.maxHeight, max(AskComposerField.minHeight, ceil(used) + AskComposerField.inset.height * 2))
            if abs(parent.height - h) > 0.5 { parent.height = h }
        }
    }
}

final class ComposerTextView: NSTextView {
    var placeholder = ""
    var onSubmit: (() -> Void)?

    override func keyDown(with event: NSEvent) {
        let isReturn = event.keyCode == 36 || event.keyCode == 76
        // An input method uses Return to commit its candidate: that keystroke is not a send.
        if isReturn && !hasMarkedText() {
            let mods = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            if mods.contains(.shift) || mods.contains(.option) {
                insertNewlineIgnoringFieldEditor(nil)
                return
            }
            if mods.isEmpty || mods == .numericPad || mods == .function {
                onSubmit?()
                return
            }
        }
        super.keyDown(with: event)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard string.isEmpty, !placeholder.isEmpty else { return }
        let pad = textContainer?.lineFragmentPadding ?? 0
        let attrs: [NSAttributedString.Key: Any] = [
            .font: font ?? NSFont.systemFont(ofSize: Metrics.fsUI),
            .foregroundColor: NSColor.placeholderTextColor,
        ]
        let origin = NSPoint(x: textContainerInset.width + pad, y: textContainerInset.height)
        (placeholder as NSString).draw(at: origin, withAttributes: attrs)
    }
}

/// The three answer-shape chips, each showing its current value (ask.js:495-571).
struct AskShapeBar: View {
    let model: AskModel

    struct Field {
        let key: String
        let help: String
        let options: [(value: String, label: String, note: String)]
    }

    static let fields: [Field] = [
        Field(
            key: "people", help: "How many people the answer names. Every match still appears below it.",
            options: [("10", "10 people", "A short answer"), ("25", "25 people", "A longer answer"),
                ("all", "All people", "Name everyone found")]),
        Field(
            key: "detail", help: "Brief writes less prose. It can stop early on a long list.",
            options: [("brief", "Brief", "One or two sentences"), ("full", "Full", "A reason for each person")]),
        Field(
            key: "search", help: "Quick makes fewer searches. It can miss people.",
            options: [("quick", "Quick", "Fewer searches. It can miss people"),
                ("thorough", "Thorough", "More searches. It takes longer")]),
    ]

    var body: some View {
        HStack(spacing: 6) {
            ForEach(Self.fields, id: \.key) { field in chip(field) }
        }
    }

    private func value(_ key: String) -> String {
        switch key {
        case "people": model.shape.people
        case "detail": model.shape.detail
        default: model.shape.search
        }
    }

    @ViewBuilder private func chip(_ field: Field) -> some View {
        // The engine decides which controls did anything on the last answer, and why not.
        let control = model.lastShape?.controls[field.key]
        let off = control.map { !$0.available } ?? (model.aiOff && field.key != "people")
        let why = control?.reason.isEmpty == false ? control!.reason : (off ? "Keyword search has one setting for this." : "")
        let current = field.options.first { $0.value == value(field.key) } ?? field.options[0]
        Menu {
            ForEach(field.options, id: \.value) { option in
                Toggle(
                    isOn: Binding(
                        get: { value(field.key) == option.value },
                        set: { if $0 { model.pickShape(field.key, option.value) } })
                ) {
                    Text(option.label)
                    Text(option.note)
                }
            }
        } label: {
            Text(current.label)
        }
        .menuStyle(.button)
        .buttonStyle(.bordered)
        .controlSize(.small)
        .fixedSize()
        .disabled(off)
        .help(off ? why : field.help)
    }
}
