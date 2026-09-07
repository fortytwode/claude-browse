import AppKit
import Foundation
import UserNotifications

private struct NotificationArguments {
    let title: String
    let message: String

    static func parse(_ arguments: [String]) -> NotificationArguments? {
        var title: String?
        var message: String?
        var index = 0
        while index < arguments.count {
            switch arguments[index] {
            case "--title" where index + 1 < arguments.count:
                title = arguments[index + 1]
                index += 2
            case "--message" where index + 1 < arguments.count:
                message = arguments[index + 1]
                index += 2
            default:
                return nil
            }
        }
        guard let title, let message else { return nil }
        return NotificationArguments(title: title, message: message)
    }
}

private final class NotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate {
    private let center = UNUserNotificationCenter.current()
    private let notificationDelegate = NotificationDelegate()

    func applicationDidFinishLaunching(_ notification: Notification) {
        center.delegate = notificationDelegate
        let arguments = Array(CommandLine.arguments.dropFirst())

        if arguments == ["--request-permission"] {
            requestPermission { [weak self] _ in
                self?.finish()
            }
            return
        }

        guard let parsed = NotificationArguments.parse(arguments) else {
            fputs("usage: AgentBoardNotifier --title TITLE --message MESSAGE\n", stderr)
            finish()
            return
        }

        requestPermission { [weak self] granted in
            guard let self else { return }
            guard granted else {
                self.finish()
                return
            }
            let content = UNMutableNotificationContent()
            content.title = parsed.title
            content.body = parsed.message
            content.sound = .default
            if #available(macOS 12.0, *) {
                content.interruptionLevel = .timeSensitive
            }
            let request = UNNotificationRequest(
                identifier: UUID().uuidString,
                content: content,
                trigger: nil
            )
            self.center.add(request) { _ in
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) {
                    self.finish()
                }
            }
        }
    }

    private func requestPermission(then completion: @escaping (Bool) -> Void) {
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            DispatchQueue.main.async {
                completion(granted)
            }
        }
    }

    private func finish() {
        NSApplication.shared.terminate(nil)
    }
}

private let application = NSApplication.shared
private let delegate = AppDelegate()
application.setActivationPolicy(.accessory)
application.delegate = delegate
application.run()
