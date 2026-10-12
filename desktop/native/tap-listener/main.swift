import CoreFoundation
import Foundation
import IOKit
import IOKit.hid

private func emit(_ fields: [String: Any]) {
    guard
        let data = try? JSONSerialization.data(withJSONObject: fields),
        let line = String(data: data, encoding: .utf8)
    else { return }
    print(line)
    fflush(stdout)
}

private final class ImpulseDetector {
    private let threshold = 0.18
    private let refractory: TimeInterval = 0.11
    private let rearmRatio = 0.30
    private let baselineAlpha = 0.02
    private var baseline = 1.0
    private var lastImpulse: TimeInterval = -.infinity
    private var armed = true
    private var peak = 0.0

    func process(magnitude: Double, at time: TimeInterval) -> Double? {
        let deviation = abs(magnitude - baseline)
        if armed && deviation < threshold * 0.5 {
            baseline += baselineAlpha * (magnitude - baseline)
        }
        if !armed {
            peak = max(peak, deviation)
            if time - lastImpulse >= refractory && deviation < peak * rearmRatio {
                armed = true
            }
            return nil
        }
        guard deviation >= threshold, time - lastImpulse >= refractory else {
            return nil
        }
        lastImpulse = time
        peak = deviation
        armed = false
        return deviation
    }
}

private enum TapGesture {
    case single
    case double
}

private final class TapRecognizer {
    private let minimumGap: TimeInterval = 0.12
    private let maximumGap: TimeInterval = 0.48
    private let cooldown: TimeInterval = 1.25
    private var first: (time: TimeInterval, strength: Double)?
    private var lastGestureAt: TimeInterval = -.infinity

    func register(time: TimeInterval, strength: Double) -> TapGesture? {
        guard time - lastGestureAt >= cooldown else { return nil }
        guard let first else {
            self.first = (time, strength)
            return nil
        }
        let gap = time - first.time
        if gap > maximumGap {
            self.first = (time, strength)
            return .single
        }
        guard gap >= minimumGap else { return nil }
        let similarity = min(first.strength, strength) / max(first.strength, strength)
        guard similarity >= 0.42 else {
            self.first = (time, strength)
            return nil
        }
        self.first = nil
        lastGestureAt = time
        return .double
    }

    func flush(at time: TimeInterval) -> TapGesture? {
        guard let first, time - first.time > maximumGap else { return nil }
        self.first = nil
        lastGestureAt = time
        return .single
    }
}

private final class SPUAccelerometer {
    static let usagePage = 0xFF00
    static let usage = 3

    var onSample: ((Double, TimeInterval) -> Void)?
    private var manager: IOHIDManager?
    private var buffers: [UnsafeMutablePointer<UInt8>] = []

    static func isSupported() -> Bool {
        let manager = IOHIDManagerCreate(
            kCFAllocatorDefault,
            IOOptionBits(kIOHIDOptionsTypeNone)
        )
        IOHIDManagerSetDeviceMatching(manager, [
            kIOHIDDeviceUsagePageKey: usagePage,
            kIOHIDDeviceUsageKey: usage,
        ] as CFDictionary)
        guard IOHIDManagerOpen(manager, IOOptionBits(kIOHIDOptionsTypeNone)) == kIOReturnSuccess else {
            return false
        }
        defer { IOHIDManagerClose(manager, IOOptionBits(kIOHIDOptionsTypeNone)) }
        return (IOHIDManagerCopyDevices(manager) as? Set<IOHIDDevice>)?.isEmpty == false
    }

    func run() {
        let manager = IOHIDManagerCreate(
            kCFAllocatorDefault,
            IOOptionBits(kIOHIDOptionsTypeNone)
        )
        self.manager = manager
        IOHIDManagerSetDeviceMatching(manager, [
            kIOHIDDeviceUsagePageKey: Self.usagePage,
            kIOHIDDeviceUsageKey: Self.usage,
        ] as CFDictionary)
        let context = Unmanaged.passUnretained(self).toOpaque()
        IOHIDManagerRegisterDeviceMatchingCallback(manager, { context, _, _, device in
            guard let context else { return }
            Unmanaged<SPUAccelerometer>
                .fromOpaque(context)
                .takeUnretainedValue()
                .attach(device: device, context: context)
        }, context)
        IOHIDManagerScheduleWithRunLoop(
            manager,
            CFRunLoopGetCurrent(),
            CFRunLoopMode.defaultMode.rawValue
        )
        guard IOHIDManagerOpen(manager, IOOptionBits(kIOHIDOptionsTypeNone)) == kIOReturnSuccess else {
            emit(["status": "error", "message": "The MacBook motion sensor could not be opened."])
            return
        }
        wakeDriver()
        emit(["status": "ready"])
        var ticks = 0
        while true {
            CFRunLoopRunInMode(CFRunLoopMode.defaultMode, 0.25, true)
            ticks += 1
            if ticks % 8 == 0 { wakeDriver() }
        }
    }

    private func wakeDriver() {
        var iterator: io_iterator_t = 0
        guard IOServiceGetMatchingServices(
            kIOMainPortDefault,
            IOServiceMatching("AppleSPUHIDDriver"),
            &iterator
        ) == KERN_SUCCESS else { return }
        defer { IOObjectRelease(iterator) }
        var service = IOIteratorNext(iterator)
        while service != 0 {
            let usage = IORegistryEntryCreateCFProperty(
                service,
                "PrimaryUsage" as CFString,
                kCFAllocatorDefault,
                0
            )?.takeRetainedValue() as? Int
            if usage == Self.usage {
                IORegistryEntrySetCFProperty(
                    service,
                    "ReportInterval" as CFString,
                    1_000 as CFNumber
                )
            }
            IOObjectRelease(service)
            service = IOIteratorNext(iterator)
        }
    }

    private func attach(device: IOHIDDevice, context: UnsafeMutableRawPointer) {
        guard IOHIDDeviceOpen(device, IOOptionBits(kIOHIDOptionsTypeNone)) == kIOReturnSuccess else {
            return
        }
        IOHIDDeviceSetProperty(
            device,
            kIOHIDReportIntervalKey as CFString,
            1_250 as CFNumber
        )
        let size = (IOHIDDeviceGetProperty(
            device,
            kIOHIDMaxInputReportSizeKey as CFString
        ) as? Int) ?? 64
        let buffer = UnsafeMutablePointer<UInt8>.allocate(capacity: size)
        buffers.append(buffer)
        IOHIDDeviceRegisterInputReportCallback(
            device,
            buffer,
            size,
            { context, result, _, _, _, report, length in
                guard result == kIOReturnSuccess, let context else { return }
                Unmanaged<SPUAccelerometer>
                    .fromOpaque(context)
                    .takeUnretainedValue()
                    .handle(report: report, length: length)
            },
            context
        )
    }

    private func handle(report: UnsafePointer<UInt8>, length: Int) {
        guard length >= 18 else { return }
        func value(at offset: Int) -> Double {
            Double(Int32(
                littleEndian: UnsafeRawPointer(report + offset)
                    .loadUnaligned(as: Int32.self)
            )) / 65_536.0
        }
        let x = value(at: 6)
        let y = value(at: 10)
        let z = value(at: 14)
        onSample?(
            (x * x + y * y + z * z).squareRoot(),
            ProcessInfo.processInfo.systemUptime
        )
    }
}

if CommandLine.arguments.contains("--self-test") {
    let double = TapRecognizer()
    precondition(double.register(time: 1.0, strength: 0.25) == nil)
    precondition(double.register(time: 1.25, strength: 0.22) == .double)

    let single = TapRecognizer()
    precondition(single.register(time: 2.0, strength: 0.25) == nil)
    precondition(single.flush(at: 2.49) == .single)

    let ringing = TapRecognizer()
    precondition(ringing.register(time: 3.0, strength: 0.25) == nil)
    precondition(ringing.register(time: 3.05, strength: 0.24) == nil)
    precondition(ringing.flush(at: 3.49) == .single)
    print("tap listener self-test passed")
    exit(EXIT_SUCCESS)
}

guard SPUAccelerometer.isSupported() else {
    emit(["status": "unsupported"])
    exit(EXIT_SUCCESS)
}

private let detector = ImpulseDetector()
private let recognizer = TapRecognizer()
private let accelerometer = SPUAccelerometer()
accelerometer.onSample = { magnitude, time in
    if let gesture = recognizer.flush(at: time), gesture == .single {
        emit(["gesture": "single"])
    }
    guard let strength = detector.process(magnitude: magnitude, at: time) else {
        return
    }
    switch recognizer.register(time: time, strength: strength) {
    case .single:
        emit(["gesture": "single"])
    case .double:
        emit(["gesture": "double"])
    case nil:
        break
    }
}
accelerometer.run()
