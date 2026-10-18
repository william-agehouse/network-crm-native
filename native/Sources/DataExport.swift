import AppKit
import Foundation
import UniformTypeIdentifiers

// Export a backup: your people and your notes as one file, in three formats, saved where
// you choose with the Mac's own save panel. The web page links straight to the download
// (static/app/data.js:509-511); the engine builds the file (crm/app.py:545-556,
// crm/export_data.py) and names it network-crm-export-YYYY-MM-DD.<ext>.

enum DataExportFormat: String, CaseIterable, Identifiable, Sendable {
    case md, csv, json
    var id: String { rawValue }

    /// The web page's words for each (data.js:509-511).
    var label: String {
        switch self {
        case .md: "A document to read"
        case .csv: "A spreadsheet"
        case .json: "For another program"
        }
    }

    var contentType: UTType {
        switch self {
        case .md: UTType(filenameExtension: "md") ?? .plainText
        case .csv: .commaSeparatedText
        case .json: .json
        }
    }
}

// The bytes come from EngineClient.rawGet (DataTransport.swift).

@MainActor
enum DataExport {
    /// The engine's own name for the file, with today's UTC date (crm/app.py:554-556).
    static func defaultName(_ format: DataExportFormat, now: Date = Date()) -> String {
        var style = Date.ISO8601FormatStyle().year().month().day()
        style.timeZone = TimeZone(identifier: "UTC")!
        return "network-crm-export-\(now.formatted(style)).\(format.rawValue)"
    }

    /// Fetch the export and save it where the person chooses. Returns the line to show
    /// ("Saved network-crm-export-….md"), or nil when they cancelled.
    static func save(_ format: DataExportFormat, app: AppModel) async -> String? {
        let fetched: (data: Data, fileName: String?)
        do {
            fetched = try await app.client.rawGet("/api/export", query: ["format": format.rawValue])
        } catch is CancellationError {
            return nil
        } catch {
            return "The backup was not made. \(LedgerSettings.sentence(error))"
        }
        let panel = NSSavePanel()
        panel.title = "Export a Backup"
        panel.message = "Your people and your notes. Messages are not included and stay on this Mac."
        panel.prompt = "Export"
        panel.nameFieldStringValue = fetched.fileName ?? defaultName(format)
        panel.allowedContentTypes = [format.contentType]
        panel.canCreateDirectories = true
        guard await present(panel) == .OK, let url = panel.url else { return nil }
        do {
            try fetched.data.write(to: url, options: .atomic)
            return "Saved \(url.lastPathComponent)"
        } catch {
            return "The file was not saved: \(error.localizedDescription) Choose another folder."
        }
    }

    /// File ▸ Export a Backup…: one save panel with the three formats in it, so the menu
    /// command does the whole thing without a detour.
    static func saveWithChoice(app: AppModel) async -> String? {
        let panel = NSSavePanel()
        panel.title = "Export a Backup"
        panel.message = "Your people and your notes. Messages are not included and stay on this Mac."
        panel.prompt = "Export"
        panel.canCreateDirectories = true
        let chooser = FormatChooser(panel: panel)
        panel.accessoryView = chooser.view
        panel.isExtensionHidden = false
        chooser.apply(.md)
        guard await present(panel) == .OK, let url = panel.url else { return nil }
        let format = chooser.format
        do {
            let fetched = try await app.client.rawGet("/api/export", query: ["format": format.rawValue])
            try fetched.data.write(to: url, options: .atomic)
            return "Saved \(url.lastPathComponent)"
        } catch is CancellationError {
            return nil
        } catch let e as EngineError {
            return "The backup was not made. \(LedgerSettings.sentence(e))"
        } catch {
            return "The file was not saved: \(error.localizedDescription) Choose another folder."
        }
    }

    private static func present(_ panel: NSSavePanel) async -> NSApplication.ModalResponse {
        if let window = NSApp.keyWindow ?? NSApp.mainWindow {
            return await withCheckedContinuation { cont in
                panel.beginSheetModal(for: window) { cont.resume(returning: $0) }
            }
        }
        return panel.runModal()
    }

    /// The Format pop-up under the save panel; changing it changes the file's extension.
    @MainActor
    final class FormatChooser: NSObject {
        let view: NSView
        private let popup: NSPopUpButton
        private weak var panel: NSSavePanel?
        private(set) var format: DataExportFormat = .md

        init(panel: NSSavePanel) {
            self.panel = panel
            popup = NSPopUpButton(frame: .zero, pullsDown: false)
            for f in DataExportFormat.allCases { popup.addItem(withTitle: "\(f.label) (.\(f.rawValue))") }
            let label = NSTextField(labelWithString: "Format:")
            let stack = NSStackView(views: [label, popup])
            stack.orientation = .horizontal
            stack.spacing = 8
            stack.edgeInsets = NSEdgeInsets(top: 10, left: 20, bottom: 10, right: 20)
            view = stack
            super.init()
            popup.target = self
            popup.action = #selector(changed(_:))
        }

        @objc private func changed(_ sender: NSPopUpButton) {
            apply(DataExportFormat.allCases[max(0, sender.indexOfSelectedItem)])
        }

        func apply(_ f: DataExportFormat) {
            format = f
            popup.selectItem(at: DataExportFormat.allCases.firstIndex(of: f) ?? 0)
            guard let panel else { return }
            panel.allowedContentTypes = [f.contentType]
            let base = (panel.nameFieldStringValue as NSString).deletingPathExtension
            panel.nameFieldStringValue = (base.isEmpty ? (DataExport.defaultName(f) as NSString).deletingPathExtension : base)
                + "." + f.rawValue
        }
    }
}
