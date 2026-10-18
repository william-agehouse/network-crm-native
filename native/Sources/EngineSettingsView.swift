import SwiftUI

// The engine this app talks to, as rows of the Settings page's "This install": how it is
// reached, whether this app started it (and so stops it on quit), what it reports about
// itself, and Reconnect. Only facts the app already holds (EngineController and the last
// /api/status), so nothing here asks the engine anything new.

struct EngineSettingsView: View {
    @AppEnvironment private var app
    @State private var busy = false

    var body: some View {
        let engine = app.engine
        let status = engine.status
        LedgerStatusRow(label: "The engine", detail: startedBy(engine)) {
            HStack(spacing: 8) {
                Circle().fill(engine.isReady ? Palette.green : Palette.danger).frame(width: 7, height: 7)
                Text(verbatim: stateText(engine.state)).fontWeight(.semibold).foregroundStyle(Palette.ink)
                Text(verbatim: engine.port == 0 ? "Private to this app" : "127.0.0.1:\(engine.port)")
                    .font(.system(size: Metrics.fsUI, design: .monospaced))
                    .foregroundStyle(Palette.muted)
                    .textSelection(.enabled)
            }
        } extra: {
            VStack(alignment: .leading, spacing: 8) {
                Grid(alignment: .leading, horizontalSpacing: 16, verticalSpacing: 4) {
                    if engine.isDevelopmentPort {
                        fact("Engine chosen by", engine.port == 0 ? "NC_NATIVE_SOCKET (development)" : "NC_NATIVE_PORT (development)")
                    } else {
                        fact("Reached through", "A file only this app and your account can open. No other program, browser or web page can reach it.")
                    }
                    fact("Build", status.map { $0.build.isEmpty ? "Not reported" : $0.build } ?? "—")
                    fact("Ask", status.map { $0.ai ? "Uses the AI engine" : "Keyword search only (no API key)" } ?? "—")
                    GridRow {
                        Text("Data").foregroundStyle(Palette.muted)
                        Text(verbatim: status.map { $0.dbPath.isEmpty ? "Not reported" : $0.dbPath } ?? "—")
                            .lineLimit(2)
                            .truncationMode(.middle)
                            .textSelection(.enabled)
                            .foregroundStyle(Palette.ink)
                    }
                }
                .font(.system(size: Metrics.fsUI))
                Button(busy ? "Reconnecting…" : "Reconnect") {
                    busy = true
                    Task {
                        await app.retry()
                        busy = false
                    }
                }
                .disabled(busy)
                .help("Ask the engine again whether it is there, and start it again if it stopped.")
            }
            .padding(.top, 4)
        }
    }

    private func fact(_ label: String, _ value: String) -> some View {
        GridRow {
            Text(verbatim: label).foregroundStyle(Palette.muted)
            Text(verbatim: value).foregroundStyle(Palette.ink).textSelection(.enabled)
        }
    }

    private func startedBy(_ engine: EngineController) -> String {
        engine.startedByThisApp
            ? "Started by this app. Quitting the app stops it."
            : engine.isReady
                ? "Started by hand for development. This app leaves it running when you quit."
                : "Not running."
    }

    private func stateText(_ state: EngineController.State) -> String {
        switch state {
        case .connecting: "Opening"
        case .settingUp: "Setting up"
        case .starting: "Starting"
        case .ready: "Answering"
        case .failed(let f): f.message
        }
    }
}
