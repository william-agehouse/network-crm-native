import AppKit
import SwiftUI

// --snapshot pictures of the two banners RootView puts above every screen: the engine not
// answering (Engine.swift `trouble`) and the database moved aside (/api/status
// db_recovery). Drawn on their own, at the window's width, because staging either state
// on a real engine means stopping it or breaking its database. SelfTest+Engine.swift
// proves the states themselves against the stand-in.

extension Snapshot {
    static func engineScenes() -> [Scene] {
        [
            Scene(
                name: "engine-banners",
                view: { _ in
                    AnyView(
                        VStack(spacing: 0) {
                            EngineTroubleBanner(trouble: EngineController.Failure(
                                message: "The engine stopped answering.",
                                advice: "What is on screen stays, but nothing can be saved until it answers again. This app checks every few seconds."))
                            DatabaseRecoveryBanner(
                                notice: "Your database could not be opened, so it was moved aside to crm.db.unreadable-20261016-1912 and a new, empty one was started. Nothing was deleted.")
                            Spacer(minLength: 0)
                        }
                        .background(Palette.panel))
                },
                size: NSSize(width: 1000, height: 150)
            ) { app in
                app.markSettled()
            },
            // The first start of a new version, while the packages install (NC-316).
            Scene(
                name: "engine-setting-up",
                view: { _ in AnyView(EngineWaitingView(sentence: EngineWaitingView.settingUp).background(Palette.panel)) },
                size: NSSize(width: 1000, height: 640)
            ) { app in
                app.markSettled()
            },
        ]
    }
}
