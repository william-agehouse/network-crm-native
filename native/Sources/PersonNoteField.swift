import AppKit
import SwiftUI

// The note composer's text field: Return saves, Shift-Return or Option-Return starts a new
// line, and Return while an input method is still composing only commits the candidate.
// An NSTextView, because a SwiftUI field can tell none of those apart. It grows with what
// is typed, up to six lines, then scrolls.

struct PersonNoteField: NSViewRepresentable {
    @Binding var text: String
    var placeholder: String
    var isEnabled: Bool
    /// Bumped (PersonStore.noteFocusToken) to put the caret here.
    var focusRequest: Int
    @Binding var height: CGFloat
    /// Whether the caret is here, for the focus ring the SwiftUI side draws.
    @Binding var focused: Bool
    var onSubmit: () -> Void

    static let font = NSFont.systemFont(ofSize: Metrics.fsUI)
    static let inset = NSSize(width: 2, height: 6)
    static let maxLines: CGFloat = 6
    static var lineHeight: CGFloat { NSLayoutManager().defaultLineHeight(for: font) }
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

        let tv = PersonNoteTextView(frame: .zero)
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
        tv.placeholder = placeholder
        tv.string = text
        tv.setAccessibilityLabel("New note")
        tv.onSubmit = { context.coordinator.parent.onSubmit() }
        tv.onFocusChange = { context.coordinator.focusChanged($0) }
        scroll.documentView = tv
        context.coordinator.textView = tv
        return scroll
    }

    func updateNSView(_ scroll: NSScrollView, context: Context) {
        let c = context.coordinator
        c.parent = self
        guard let tv = c.textView else { return }
        if tv.string != text {
            tv.string = text
            tv.needsDisplay = true
        }
        tv.isEditable = isEnabled
        tv.textColor = isEnabled ? .labelColor : .secondaryLabelColor
        if tv.placeholder != placeholder {
            tv.placeholder = placeholder
            tv.needsDisplay = true
        }
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
        var parent: PersonNoteField
        weak var textView: PersonNoteTextView?
        var lastFocus: Int

        init(_ parent: PersonNoteField) {
            self.parent = parent
            lastFocus = parent.focusRequest
        }

        func textDidChange(_ notification: Notification) {
            guard let tv = textView else { return }
            parent.text = tv.string
            tv.needsDisplay = true
            measure()
        }

        func focusChanged(_ on: Bool) {
            if parent.focused != on { parent.focused = on }
        }

        func measure() {
            guard let tv = textView, let lm = tv.layoutManager, let tc = tv.textContainer else { return }
            lm.ensureLayout(for: tc)
            let used = lm.usedRect(for: tc).height
            let h = min(PersonNoteField.maxHeight, max(PersonNoteField.minHeight, ceil(used) + PersonNoteField.inset.height * 2))
            if abs(parent.height - h) > 0.5 { parent.height = h }
        }
    }
}

final class PersonNoteTextView: NSTextView {
    var placeholder = ""
    var onSubmit: (() -> Void)?
    var onFocusChange: ((Bool) -> Void)?

    override func keyDown(with event: NSEvent) {
        let isReturn = event.keyCode == 36 || event.keyCode == 76
        if isReturn && !hasMarkedText() {
            let mods = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            if mods.contains(.shift) || mods.contains(.option) {
                insertNewlineIgnoringFieldEditor(nil)
                return
            }
            if mods.isEmpty || mods == .numericPad || mods == .function || mods == .command {
                onSubmit?()
                return
            }
        }
        super.keyDown(with: event)
    }

    override func becomeFirstResponder() -> Bool {
        let ok = super.becomeFirstResponder()
        if ok { onFocusChange?(true) }
        return ok
    }

    override func resignFirstResponder() -> Bool {
        let ok = super.resignFirstResponder()
        if ok { onFocusChange?(false) }
        return ok
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
