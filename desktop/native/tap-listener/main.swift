import ApplicationServices
import CoreGraphics
import Foundation

private func emit(_ fields: [String: Any]) {
    guard
        let data = try? JSONSerialization.data(withJSONObject: fields),
        let line = String(data: data, encoding: .utf8)
    else { return }
    print(line)
    fflush(stdout)
}

/// A press and release of fn on its own. fn held as a modifier for another
/// key (fn+F5, fn+arrows, fn+⌘…) or held down for a while is not a tap.
private final class FnTapRecognizer {
    private let maximumHold: TimeInterval = 0.6
    private var downAt: TimeInterval?
    private var usedAsModifier = false

    func fn(down: Bool, at time: TimeInterval) -> Bool {
        if down {
            if downAt == nil {
                downAt = time
                usedAsModifier = false
            }
            return false
        }
        guard let start = downAt else { return false }
        downAt = nil
        return !usedAsModifier && time - start <= maximumHold
    }

    func otherInput() {
        if downAt != nil { usedAsModifier = true }
    }
}

private let fnKeyCode: Int64 = 63

if CommandLine.arguments.contains("--self-test") {
    let tap = FnTapRecognizer()
    precondition(!tap.fn(down: true, at: 1.0))
    precondition(tap.fn(down: false, at: 1.15))

    let chord = FnTapRecognizer()
    precondition(!chord.fn(down: true, at: 2.0))
    chord.otherInput()
    precondition(!chord.fn(down: false, at: 2.1))

    let held = FnTapRecognizer()
    precondition(!held.fn(down: true, at: 3.0))
    precondition(!held.fn(down: false, at: 4.0))
    print("fn listener self-test passed")
    exit(EXIT_SUCCESS)
}

// Listening to keys needs Input Monitoring. Asking shows the macOS prompt the
// first time; macOS applies a new grant only after GNSIS reopens.
guard CGPreflightListenEventAccess() || CGRequestListenEventAccess() else {
    emit([
        "status": "error",
        "message": "Allow GNSIS in System Settings → Privacy & Security → Input Monitoring, then reopen GNSIS.",
    ])
    exit(EXIT_SUCCESS)
}

private let recognizer = FnTapRecognizer()
private var eventTap: CFMachPort?

private let mask: CGEventMask =
    (1 << CGEventType.flagsChanged.rawValue) | (1 << CGEventType.keyDown.rawValue)

eventTap = CGEvent.tapCreate(
    tap: .cgSessionEventTap,
    place: .headInsertEventTap,
    options: .listenOnly,
    eventsOfInterest: mask,
    callback: { _, type, event, _ in
        switch type {
        case .flagsChanged:
            if event.getIntegerValueField(.keyboardEventKeycode) == fnKeyCode {
                let down = event.flags.contains(.maskSecondaryFn)
                if recognizer.fn(down: down, at: ProcessInfo.processInfo.systemUptime) {
                    // The wake gesture: GNSIS comes forward and toggles listening.
                    emit(["gesture": "double"])
                }
            } else {
                recognizer.otherInput()
            }
        case .keyDown:
            recognizer.otherInput()
        case .tapDisabledByTimeout, .tapDisabledByUserInput:
            if let eventTap { CGEvent.tapEnable(tap: eventTap, enable: true) }
        default:
            break
        }
        return Unmanaged.passUnretained(event)
    },
    userInfo: nil
)

guard let activeTap = eventTap else {
    emit(["status": "error", "message": "The fn key listener could not start."])
    exit(EXIT_SUCCESS)
}
CFRunLoopAddSource(
    CFRunLoopGetCurrent(),
    CFMachPortCreateRunLoopSource(kCFAllocatorDefault, activeTap, 0),
    .commonModes
)
CGEvent.tapEnable(tap: activeTap, enable: true)
emit(["status": "ready"])
CFRunLoopRun()
