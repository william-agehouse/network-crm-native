import SwiftUI

// The Data page's commands that the menus and ⌘K run (File ▸ Find Duplicates…, File ▸
// Export a Backup…). The web page's equivalents are links to #data?dup=1 and
// #data?export=1 (static/app/main.js:179-184), which open the page on that part.

@MainActor
enum CommandActions {
    /// Open the Data page on its possible duplicates, with the groups shown.
    static func findDuplicates(_ app: AppModel) {
        app.dataFocus = "duplicates"
        app.section = .data
    }

    /// Open the Data page on its export, and start the export straight away: one save
    /// panel with the three formats in it.
    static func exportBackup(_ app: AppModel) {
        app.dataFocus = "export"
        app.section = .data
        Task { @MainActor in
            // Let the page come up behind the panel first.
            try? await Task.sleep(for: .milliseconds(250))
            if let line = await DataExport.saveWithChoice(app: app) { DataSettingsBus.shared.announce(line) }
        }
    }
}
