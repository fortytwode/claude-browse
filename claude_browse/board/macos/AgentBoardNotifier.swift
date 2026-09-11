import AppKit
import Darwin
import Foundation
import UserNotifications

private let notificationRequestName = Notification.Name("com.fortytwode.agent-board-notifier.request")
private let sessionIDKey = "sessionID"
private let providerKey = "provider"

private struct NotificationArguments {
    let title: String
    let message: String
    let sessionID: String?
    let provider: String?

    static func parse(_ arguments: [String]) -> NotificationArguments? {
        var values: [String: String] = [:]
        var index = 0
        while index < arguments.count {
            guard index + 1 < arguments.count else { return nil }
            let key = arguments[index]
            guard ["--title", "--message", "--session-id", "--provider"].contains(key) else {
                return nil
            }
            values[key] = arguments[index + 1]
            index += 2
        }
        guard let title = values["--title"], let message = values["--message"] else {
            return nil
        }
        let provider = values["--provider"]
        if provider != nil && provider != "claude" && provider != "codex" { return nil }
        return NotificationArguments(
            title: title,
            message: message,
            sessionID: values["--session-id"],
            provider: provider
        )
    }

    var userInfo: [AnyHashable: Any] {
        var info: [AnyHashable: Any] = [:]
        if let sessionID { info[sessionIDKey] = sessionID }
        if let provider { info[providerKey] = provider }
        return info
    }

    var distributedUserInfo: [AnyHashable: Any] {
        var info = userInfo
        info["title"] = title
        info["message"] = message
        return info
    }

    static func fromDistributed(_ info: [AnyHashable: Any]?) -> NotificationArguments? {
        guard
            let info,
            let title = info["title"] as? String,
            let message = info["message"] as? String
        else { return nil }
        return NotificationArguments(
            title: title,
            message: message,
            sessionID: info[sessionIDKey] as? String,
            provider: info[providerKey] as? String
        )
    }
}

private final class NotificationDelegate: NSObject, UNUserNotificationCenterDelegate {
    var responseHandler: ((UNNotificationResponse) -> Void)?

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification,
        withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void
    ) {
        completionHandler([.banner, .list, .sound])
    }

    func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse,
        withCompletionHandler completionHandler: @escaping () -> Void
    ) {
        responseHandler?(response)
        completionHandler()
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate {
    private let center = UNUserNotificationCenter.current()
    private let notificationDelegate = NotificationDelegate()
    private var pending: [String: [AnyHashable: Any]] = [:]
    private var pendingOrder: [String] = []
    private var lockFileDescriptor: Int32 = -1
    private var authorizationGranted: Bool?
    private var authorizationRequestInFlight = false
    private var authorizationWaiters: [(Bool) -> Void] = []
    private var awaitingAuthorization: [NotificationArguments] = []

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.regular)
        center.delegate = notificationDelegate
        notificationDelegate.responseHandler = { [weak self] response in
            self?.review(
                response.notification.request.identifier,
                target: response.notification.request.content.userInfo
            )
        }
        DistributedNotificationCenter.default().addObserver(
            self,
            selector: #selector(receiveDistributedRequest(_:)),
            name: notificationRequestName,
            object: nil
        )

        let arguments = Array(CommandLine.arguments.dropFirst())
        if arguments == ["--request-permission"] {
            guard acquireSingleton() else { finish(); return }
            ensureAuthorization { [weak self] _ in self?.restoreDeliveredNotifications() }
            return
        }
        if arguments.isEmpty {
            guard acquireSingleton() else { finish(); return }
            restoreDeliveredNotifications()
            return
        }
        guard let parsed = NotificationArguments.parse(arguments) else {
            fputs("usage: AgentBoardNotifier --title TITLE --message MESSAGE [--session-id ID --provider claude|codex]\n", stderr)
            finish()
            return
        }

        if !acquireSingleton() {
            DistributedNotificationCenter.default().postNotificationName(
                notificationRequestName,
                object: nil,
                userInfo: parsed.distributedUserInfo,
                deliverImmediately: true
            )
            finish()
            return
        }
        restoreDeliveredNotifications { [weak self] in self?.submit(parsed) }
    }

    func applicationShouldHandleReopen(
        _ sender: NSApplication,
        hasVisibleWindows flag: Bool
    ) -> Bool {
        if let identifier = pendingOrder.last { review(identifier, target: pending[identifier]) }
        return false
    }

    @objc private func receiveDistributedRequest(_ notification: Notification) {
        guard let arguments = NotificationArguments.fromDistributed(notification.userInfo) else { return }
        submit(arguments)
    }

    private func submit(_ arguments: NotificationArguments) {
        awaitingAuthorization.append(arguments)
        ensureAuthorization { [weak self] granted in
            guard let self else { return }
            let queued = self.awaitingAuthorization
            self.awaitingAuthorization.removeAll()
            for arguments in queued {
                if granted { self.deliver(arguments) }
                else { self.fallback(arguments) }
            }
        }
    }

    private func deliver(_ arguments: NotificationArguments) {
        let content = UNMutableNotificationContent()
        content.title = arguments.title
        content.body = arguments.message
        content.sound = .default
        content.userInfo = arguments.userInfo
        if #available(macOS 12.0, *) { content.interruptionLevel = .timeSensitive }
        if let sessionID = arguments.sessionID {
            let replaced = removePending(sessionID: sessionID)
            center.removeDeliveredNotifications(withIdentifiers: replaced)
        }
        let identifier = UUID().uuidString
        let request = UNNotificationRequest(identifier: identifier, content: content, trigger: nil)
        pending[identifier] = arguments.userInfo
        pendingOrder.append(identifier)
        updateBadge()
        center.add(request) { error in
            DispatchQueue.main.async {
                if error != nil {
                    self.pending.removeValue(forKey: identifier)
                    self.pendingOrder.removeAll { $0 == identifier }
                    self.updateBadge()
                    self.fallback(arguments)
                    return
                }
            }
        }
    }

    private func restoreDeliveredNotifications(then completion: @escaping () -> Void = {}) {
        center.getDeliveredNotifications { [weak self] notifications in
            DispatchQueue.main.async {
                guard let self else { completion(); return }
                for notification in notifications.sorted(by: { $0.date < $1.date }) {
                    let identifier = notification.request.identifier
                    let info = notification.request.content.userInfo
                    guard let sessionID = info[sessionIDKey] as? String else { continue }
                    let replaced = self.removePending(sessionID: sessionID)
                    self.center.removeDeliveredNotifications(withIdentifiers: replaced)
                    if self.pending[identifier] == nil { self.pendingOrder.append(identifier) }
                    self.pending[identifier] = info
                }
                self.updateBadge()
                completion()
            }
        }
    }

    private func removePending(sessionID: String) -> [String] {
        let identifiers = pending.compactMap { identifier, target in
            target[sessionIDKey] as? String == sessionID ? identifier : nil
        }
        for identifier in identifiers { pending.removeValue(forKey: identifier) }
        pendingOrder.removeAll { identifiers.contains($0) }
        return identifiers
    }

    private func review(_ identifier: String, target: [AnyHashable: Any]?) {
        guard let target = target ?? pending[identifier] else { return }
        focus(target)
        pending.removeValue(forKey: identifier)
        pendingOrder.removeAll { $0 == identifier }
        center.removeDeliveredNotifications(withIdentifiers: [identifier])
        updateBadge()
    }

    private func focus(_ target: [AnyHashable: Any]) {
        guard
            let command = Bundle.main.object(forInfoDictionaryKey: "AgentBoardFocusCommand") as? String,
            command.hasPrefix("/"),
            let sessionID = target[sessionIDKey] as? String,
            let provider = target[providerKey] as? String,
            provider == "claude" || provider == "codex"
        else { return }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: command)
        process.arguments = ["focus-session", sessionID, provider]
        try? process.run()
    }

    private func updateBadge() {
        NSApplication.shared.dockTile.badgeLabel = pending.isEmpty ? nil : String(pending.count)
        if pending.isEmpty { NSApplication.shared.setActivationPolicy(.accessory) }
        else { NSApplication.shared.setActivationPolicy(.regular) }
    }

    private func ensureAuthorization(then completion: @escaping (Bool) -> Void) {
        if let authorizationGranted { completion(authorizationGranted); return }
        authorizationWaiters.append(completion)
        if authorizationRequestInFlight { return }
        authorizationRequestInFlight = true
        center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
            DispatchQueue.main.async {
                self.authorizationGranted = granted
                self.authorizationRequestInFlight = false
                let waiters = self.authorizationWaiters
                self.authorizationWaiters.removeAll()
                for waiter in waiters { waiter(granted) }
            }
        }
    }

    private func acquireSingleton() -> Bool {
        let path = NSTemporaryDirectory() + "agent-board-notifier-\(getuid()).lock"
        let descriptor = open(path, O_CREAT | O_RDWR, S_IRUSR | S_IWUSR)
        guard descriptor >= 0 else { return false }
        guard flock(descriptor, LOCK_EX | LOCK_NB) == 0 else {
            close(descriptor)
            return false
        }
        lockFileDescriptor = descriptor
        return true
    }

    private func finish() {
        NSApplication.shared.terminate(nil)
    }

    private func fallback(_ arguments: NotificationArguments) {
        let quote: (String) -> String = { value in
            "\"" + value.replacingOccurrences(of: "\\", with: "\\\\")
                .replacingOccurrences(of: "\"", with: "\\\"") + "\""
        }
        let script = "display notification \(quote(arguments.message)) "
            + "with title \(quote(arguments.title)) sound name \"default\""
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        process.arguments = ["-e", script]
        try? process.run()
    }
}

private let application = NSApplication.shared
private let delegate = AppDelegate()
application.delegate = delegate
application.run()
