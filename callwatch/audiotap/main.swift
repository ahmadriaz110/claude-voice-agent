// audiotap - CoreAudio helper for call_watch.py (macOS 14.2+, built here on 26.x)
//
//   audiotap list                        every CoreAudio process object as JSON
//   audiotap detect                      only Teams / WhatsApp processes, plus "active"
//   audiotap record --out DIR [opts]     record the mic and a process tap to 16 kHz mono WAV chunks
//
// record options
//   --app Teams|WhatsApp      tap every process object of that app (main + helpers), refreshed
//   --pid N                   tap this pid (repeatable; combined with --app)
//   --no-tap / --no-mic       skip one side
//   --chunk-seconds N         chunk length (default 600)
//   --rate N                  output sample rate (default 16000)
//   --seconds N               stop after N seconds (tests)
//   --start-index N           first chunk number (default 1) - lets the daemon resume numbering
//   --mic-device SUBSTR       input device name match (empty or absent: the default input)
//   --no-disclaim             do not re-spawn as our own TCC "responsible process"
//
// Files: DIR/mic-NNN.wav, DIR/remote-NNN.wav, DIR/mix-NNN.wav (mix only when both sides run).
// Progress goes to stdout as JSON lines; errors to stderr. SIGTERM/SIGINT stop cleanly.
//
// Detection: kAudioHardwarePropertyProcessObjectList -> per object PID, BundleID,
// IsRunningInput, IsRunningOutput. The executable path (proc_pidpath) decides which app a
// helper process belongs to; bundle IDs of helpers vary.
//
// Remote side: CATapDescription(monoMixdownOfProcesses:) + AudioHardwareCreateProcessTap, wrapped
// in a private aggregate device whose only sub-device is the default output (clock source) and
// whose tap list holds the tap; an IOProc on the aggregate receives the tapped audio. On macOS 26
// the description also carries bundleIDs + processRestoreEnabled so helpers that (re)spawn during
// the call are picked up; the process list is refreshed every 5 s as well.
//
// Mic side: an IOProc directly on the input device (HAL shares it with other clients).
//
// Both sides are converted to 16 kHz mono Int16 with AVAudioConverter on a writer thread; the
// realtime callbacks only append to locked arrays. WAV headers are patched every flush so a hard
// kill still leaves readable files. Memory is bounded: buffers are drained every 200 ms.
//
// TCC: the first tap needs "System Audio Recording Only" (Privacy & Security > Screen & System
// Audio Recording) and the mic needs Microphone. Unless --no-disclaim is given, record re-spawns
// itself with responsibility_spawnattrs_setdisclaim so the prompt and the grant are attributed
// to this binary, not to whichever python/terminal launched it.

import Foundation
import CoreAudio
import AVFoundation
import Darwin

// MARK: - small CoreAudio property helpers

func address(_ sel: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
             _ elem: AudioObjectPropertyElement = kAudioObjectPropertyElementMain) -> AudioObjectPropertyAddress {
    return AudioObjectPropertyAddress(mSelector: sel, mScope: scope, mElement: elem)
}

func getUInt32(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
               _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> UInt32? {
    var addr = address(sel, scope)
    var val: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    let st = AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &val)
    return st == noErr ? val : nil
}

func getFloat64(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
                _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> Double? {
    var addr = address(sel, scope)
    var val: Double = 0
    var size = UInt32(MemoryLayout<Double>.size)
    let st = AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &val)
    return st == noErr ? val : nil
}

func getString(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
               _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> String? {
    var addr = address(sel, scope)
    var size = UInt32(MemoryLayout<CFString?>.size)
    var value: Unmanaged<CFString>? = nil
    let st = withUnsafeMutablePointer(to: &value) { ptr -> OSStatus in
        return AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, ptr)
    }
    guard st == noErr, let v = value else { return nil }
    return v.takeRetainedValue() as String
}

func getObjectIDs(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
                  _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> [AudioObjectID] {
    var addr = address(sel, scope)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(obj, &addr, 0, nil, &size) == noErr, size > 0 else { return [] }
    let n = Int(size) / MemoryLayout<AudioObjectID>.size
    var ids = [AudioObjectID](repeating: 0, count: n)
    let st = ids.withUnsafeMutableBufferPointer { buf -> OSStatus in
        return AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, buf.baseAddress!)
    }
    return st == noErr ? ids : []
}

func getASBD(_ obj: AudioObjectID, _ sel: AudioObjectPropertySelector,
             _ scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal) -> AudioStreamBasicDescription? {
    var addr = address(sel, scope)
    var asbd = AudioStreamBasicDescription()
    var size = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
    let st = AudioObjectGetPropertyData(obj, &addr, 0, nil, &size, &asbd)
    return st == noErr ? asbd : nil
}

func pidPath(_ pid: pid_t) -> String {
    var buf = [CChar](repeating: 0, count: 4 * Int(MAXPATHLEN))
    let n = proc_pidpath(pid, &buf, UInt32(buf.count))
    if n <= 0 { return "" }
    return String(cString: buf)
}

func pidName(_ pid: pid_t) -> String {
    var buf = [CChar](repeating: 0, count: 256)
    let n = proc_name(pid, &buf, UInt32(buf.count))
    if n <= 0 { return "" }
    return String(cString: buf)
}

func osstatusText(_ st: OSStatus) -> String {
    let u = UInt32(bitPattern: st)
    let bytes = [UInt8((u >> 24) & 0xff), UInt8((u >> 16) & 0xff), UInt8((u >> 8) & 0xff), UInt8(u & 0xff)]
    if bytes.allSatisfy({ $0 >= 32 && $0 < 127 }) {
        return "\(st) ('\(String(decoding: bytes, as: UTF8.self))')"
    }
    return "\(st)"
}

// MARK: - process objects

struct ProcInfo {
    var object: AudioObjectID
    var pid: pid_t
    var bundle: String
    var path: String
    var name: String
    var runningInput: Bool
    var runningOutput: Bool
    var app: String?          // "Teams" / "WhatsApp" / nil

    var json: [String: Any] {
        return ["object": Int(object), "pid": Int(pid), "bundle": bundle, "path": path, "name": name,
                "running_input": runningInput, "running_output": runningOutput, "app": app ?? NSNull()]
    }
}

let debugMatchName = ProcessInfo.processInfo.environment["AUDIOTAP_MATCH_NAME"]   // tests: treat this process name as "Teams"

func classify(bundle: String, path: String, name: String = "") -> String? {
    if let d = debugMatchName, !d.isEmpty, name == d { return "Teams" }
    let b = bundle.lowercased()
    let p = path
    if b.hasPrefix("com.microsoft.teams") || p.contains("/Microsoft Teams.app/") || p.contains("/Microsoft Teams") {
        return "Teams"
    }
    if b == "net.whatsapp.whatsapp" || b.hasPrefix("net.whatsapp.") || p.contains("/WhatsApp.app/") {
        return "WhatsApp"
    }
    return nil
}

func processObjects() -> [ProcInfo] {
    let ids = getObjectIDs(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyProcessObjectList)
    var out: [ProcInfo] = []
    for id in ids {
        var pid: pid_t = 0
        var addr = address(kAudioProcessPropertyPID)
        var size = UInt32(MemoryLayout<pid_t>.size)
        if AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &pid) != noErr { continue }
        let bundle = getString(id, kAudioProcessPropertyBundleID) ?? ""
        let path = pidPath(pid)
        let name = pidName(pid)
        let inp = (getUInt32(id, kAudioProcessPropertyIsRunningInput) ?? 0) != 0
        let outp = (getUInt32(id, kAudioProcessPropertyIsRunningOutput) ?? 0) != 0
        out.append(ProcInfo(object: id, pid: pid, bundle: bundle, path: path, name: name,
                            runningInput: inp, runningOutput: outp, app: classify(bundle: bundle, path: path, name: name)))
    }
    return out
}

func printJSON(_ obj: Any) {
    if let d = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]),
       let s = String(data: d, encoding: .utf8) {
        print(s)
        fflush(stdout)
    }
}

func emit(_ obj: [String: Any]) {
    var o = obj
    o["t"] = Date().timeIntervalSince1970
    printJSON(o)
}

func warn(_ s: String) {
    FileHandle.standardError.write((s + "\n").data(using: .utf8)!)
}

// MARK: - WAV writer (16-bit PCM, header patched on every flush)

final class WavWriter {
    let url: URL
    let rate: Int
    let channels: Int
    private var fh: FileHandle
    private(set) var frames: Int = 0
    private var dirty = false

    init(url: URL, rate: Int, channels: Int = 1) throws {
        self.url = url; self.rate = rate; self.channels = channels
        FileManager.default.createFile(atPath: url.path, contents: nil)
        fh = try FileHandle(forWritingTo: url)
        try fh.write(contentsOf: WavWriter.header(rate: rate, channels: channels, dataBytes: 0))
    }

    static func header(rate: Int, channels: Int, dataBytes: Int) -> Data {
        var d = Data()
        func u32(_ v: UInt32) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 4)) }
        func u16(_ v: UInt16) { var x = v.littleEndian; d.append(Data(bytes: &x, count: 2)) }
        d.append("RIFF".data(using: .ascii)!); u32(UInt32(36 + dataBytes))
        d.append("WAVE".data(using: .ascii)!)
        d.append("fmt ".data(using: .ascii)!); u32(16); u16(1); u16(UInt16(channels))
        u32(UInt32(rate)); u32(UInt32(rate * channels * 2)); u16(UInt16(channels * 2)); u16(16)
        d.append("data".data(using: .ascii)!); u32(UInt32(dataBytes))
        return d
    }

    func append(_ samples: [Int16]) {
        if samples.isEmpty { return }
        samples.withUnsafeBufferPointer { p in
            fh.write(Data(buffer: p))
        }
        frames += samples.count / channels
        dirty = true
    }

    func flush() {
        guard dirty else { return }
        dirty = false
        let pos = fh.offsetInFile
        fh.seek(toFileOffset: 0)
        fh.write(WavWriter.header(rate: rate, channels: channels, dataBytes: frames * channels * 2))
        fh.seek(toFileOffset: pos)
    }

    func close() {
        dirty = true
        flush()
        try? fh.close()
    }
}

// MARK: - streaming resampler (Float32 mono @ inRate -> Int16 mono @ outRate)

final class Resampler {
    let inFmt: AVAudioFormat
    let outFmt: AVAudioFormat
    let conv: AVAudioConverter

    init?(inRate: Double, outRate: Double) {
        guard let i = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: inRate, channels: 1, interleaved: false),
              let o = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: outRate, channels: 1, interleaved: true),
              let c = AVAudioConverter(from: i, to: o) else { return nil }
        inFmt = i; outFmt = o; conv = c
        conv.sampleRateConverterQuality = AVAudioQuality.medium.rawValue
    }

    func process(_ input: [Float]) -> [Int16] {
        if input.isEmpty { return [] }
        guard let inBuf = AVAudioPCMBuffer(pcmFormat: inFmt, frameCapacity: AVAudioFrameCount(input.count)) else { return [] }
        inBuf.frameLength = AVAudioFrameCount(input.count)
        input.withUnsafeBufferPointer { p in
            inBuf.floatChannelData![0].update(from: p.baseAddress!, count: input.count)
        }
        let cap = AVAudioFrameCount(Double(input.count) * outFmt.sampleRate / inFmt.sampleRate) + 64
        guard let outBuf = AVAudioPCMBuffer(pcmFormat: outFmt, frameCapacity: cap) else { return [] }
        var consumed = false
        var err: NSError?
        let status = conv.convert(to: outBuf, error: &err) { _, outStatus in
            if consumed { outStatus.pointee = .noDataNow; return nil }
            consumed = true; outStatus.pointee = .haveData; return inBuf
        }
        if status == .error { warn("resampler: \(err?.localizedDescription ?? "error")"); return [] }
        let n = Int(outBuf.frameLength)
        if n == 0 { return [] }
        return Array(UnsafeBufferPointer(start: outBuf.int16ChannelData![0], count: n))
    }
}

// MARK: - one recorded side (mic or remote): realtime append, writer-thread drain

final class Side {
    let name: String
    let lock = NSLock()
    var pending: [Float] = []          // mono float at device rate, filled by the IOProc
    var deviceRate: Double = 0
    var resampler: Resampler?
    var out16: [Int16] = []            // converted, not yet consumed by the mixer
    var totalOut: Int = 0              // frames at outRate ever produced
    var writer: WavWriter?
    var chunkIndex: Int
    var callbacks: Int = 0
    var dropped: Int = 0

    init(name: String, startIndex: Int) { self.name = name; self.chunkIndex = startIndex - 1 }

    // Called on the realtime thread: mix all channels to mono and append.
    func ingest(_ abl: UnsafePointer<AudioBufferList>, frames: Int) {
        let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: abl))
        guard list.count > 0, frames > 0 else { return }
        var mono = [Float](repeating: 0, count: frames)
        var chans = 0
        for buf in list {
            let nch = Int(buf.mNumberChannels)
            guard nch > 0, let data = buf.mData else { continue }
            let p = data.assumingMemoryBound(to: Float.self)
            let avail = Int(buf.mDataByteSize) / MemoryLayout<Float>.size
            let n = min(frames, avail / nch)
            for f in 0..<n {
                var s: Float = 0
                for c in 0..<nch { s += p[f * nch + c] }
                mono[f] += s
            }
            chans += nch
        }
        if chans > 1 { let g = 1.0 / Float(chans); for i in 0..<frames { mono[i] *= g } }
        lock.lock()
        callbacks += 1
        if pending.count > Int(deviceRate * 30) {     // >30 s unconsumed: writer is stuck, drop
            dropped += frames
        } else {
            pending.append(contentsOf: mono)
        }
        lock.unlock()
    }

    // Writer thread: convert whatever accumulated.
    func drain() -> [Int16] {
        lock.lock()
        let chunk = pending; pending.removeAll(keepingCapacity: true)
        lock.unlock()
        guard let r = resampler else { return [] }
        return r.process(chunk)
    }
}

final class Counter {
    private var n = 0
    private let lock = NSLock()
    func next() -> Int { lock.lock(); defer { lock.unlock() }; n += 1; return n - 1 }
}

// MARK: - the recorder

final class Recorder {
    let outDir: URL
    let outRate: Int
    let chunkFrames: Int
    let app: String?
    var extraPids: [pid_t]
    let wantTap: Bool
    let wantMic: Bool
    let micMatch: String

    var mic = Side(name: "mic", startIndex: 1)
    var remote = Side(name: "remote", startIndex: 1)
    var mixWriter: WavWriter?
    var mixIndex: Int
    var mixFrames: Int = 0
    var mixPending: (mic: [Int16], remote: [Int16]) = ([], [])

    var micDevice: AudioObjectID = 0
    var micProc: AudioDeviceIOProcID?
    var tapID: AudioObjectID = 0
    var aggID: AudioObjectID = 0
    var aggProc: AudioDeviceIOProcID?
    var tappedObjects: [AudioObjectID] = []
    var tapDescription: CATapDescription?
    let ioQueue = DispatchQueue(label: "audiotap.io", qos: .userInteractive)

    var running = true
    let writerQueue = DispatchQueue(label: "audiotap.writer", qos: .utility)
    var writerTimer: DispatchSourceTimer?
    var refreshTimer: DispatchSourceTimer?
    let startedAt = Date()
    var lastFlush = Date()

    init(outDir: URL, outRate: Int, chunkSeconds: Int, app: String?, pids: [pid_t], tap: Bool, mic: Bool,
         startIndex: Int, micMatch: String) {
        self.outDir = outDir; self.outRate = outRate; self.chunkFrames = chunkSeconds * outRate
        self.app = app; self.extraPids = pids; self.wantTap = tap; self.wantMic = mic; self.micMatch = micMatch
        self.mic = Side(name: "mic", startIndex: startIndex)
        self.remote = Side(name: "remote", startIndex: startIndex)
        self.mixIndex = startIndex - 1
    }

    // ---- devices

    func findMicDevice() -> AudioObjectID {
        let devs = getObjectIDs(AudioObjectID(kAudioObjectSystemObject), kAudioHardwarePropertyDevices)
        var fallback: AudioObjectID = 0
        var addr = address(kAudioHardwarePropertyDefaultInputDevice)
        var def: AudioObjectID = 0
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        if AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &def) == noErr { fallback = def }
        if micMatch.isEmpty { return fallback }
        for d in devs {
            let name = getString(d, kAudioObjectPropertyName) ?? ""
            let streams = getObjectIDs(d, kAudioDevicePropertyStreams, kAudioObjectPropertyScopeInput)
            if !streams.isEmpty && name.localizedCaseInsensitiveContains(micMatch) { return d }
        }
        return fallback
    }

    func defaultOutputUID(system: Bool = false) -> String? {
        var addr = address(system ? kAudioHardwarePropertyDefaultSystemOutputDevice : kAudioHardwarePropertyDefaultOutputDevice)
        var dev: AudioObjectID = 0
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &dev) == noErr else { return nil }
        return getString(dev, kAudioDevicePropertyDeviceUID)
    }

    func targetObjects() -> ([AudioObjectID], [String], [pid_t]) {
        var objs: [AudioObjectID] = []
        var bundles = Set<String>()
        var pids: [pid_t] = []
        for p in processObjects() {
            let byApp = (app != nil && p.app == app)
            let byPid = extraPids.contains(p.pid)
            if byApp || byPid {
                objs.append(p.object); pids.append(p.pid)
                if !p.bundle.isEmpty { bundles.insert(p.bundle) }
            }
        }
        return (objs, Array(bundles).sorted(), pids)
    }

    // ---- start

    func startMic() -> Bool {
        micDevice = findMicDevice()
        guard micDevice != 0 else { warn("mic: no input device"); return false }
        let name = getString(micDevice, kAudioObjectPropertyName) ?? "?"
        let rate = getFloat64(micDevice, kAudioDevicePropertyNominalSampleRate) ?? 48000
        mic.deviceRate = rate
        mic.resampler = Resampler(inRate: rate, outRate: Double(outRate))
        if let fmt = getASBD(getObjectIDs(micDevice, kAudioDevicePropertyStreams, kAudioObjectPropertyScopeInput).first ?? 0,
                             kAudioStreamPropertyVirtualFormat),
           fmt.mFormatID != kAudioFormatLinearPCM || (fmt.mFormatFlags & kAudioFormatFlagIsFloat) == 0 {
            warn("mic: unexpected stream format id=\(fmt.mFormatID) flags=\(fmt.mFormatFlags); expecting Float32")
        }
        let side = mic
        let st = AudioDeviceCreateIOProcIDWithBlock(&micProc, micDevice, ioQueue) { _, inData, _, _, _ in
            let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
            guard let first = list.first, first.mNumberChannels > 0 else { return }
            let frames = Int(first.mDataByteSize) / (MemoryLayout<Float>.size * Int(first.mNumberChannels))
            side.ingest(inData, frames: frames)
        }
        guard st == noErr, let p = micProc else { warn("mic: IOProc failed \(osstatusText(st))"); return false }
        let st2 = AudioDeviceStart(micDevice, p)
        guard st2 == noErr else { warn("mic: start failed \(osstatusText(st2))"); return false }
        emit(["event": "mic", "device": name, "rate": rate])
        return true
    }

    func startTap() -> Bool {
        let (objs, bundles, pids) = targetObjects()
        if objs.isEmpty && bundles.isEmpty {
            warn("tap: no process objects for app=\(app ?? "-") pids=\(extraPids)")
            return false
        }
        let env = ProcessInfo.processInfo.environment
        let desc: CATapDescription
        if env["AUDIOTAP_GLOBAL"] != nil {
            desc = CATapDescription(monoGlobalTapButExcludeProcesses: [])
        } else if env["AUDIOTAP_STEREO"] != nil {
            desc = CATapDescription(stereoMixdownOfProcesses: objs)
        } else {
            desc = CATapDescription(monoMixdownOfProcesses: objs)
        }
        desc.name = "callwatch-\(app ?? "pid")"
        desc.uuid = UUID()
        desc.muteBehavior = .unmuted
        if env["AUDIOTAP_NOPRIVATE"] == nil { desc.isPrivate = true }
        if #available(macOS 26.0, *), env["AUDIOTAP_NOBUNDLE"] == nil {
            desc.bundleIDs = bundles
            desc.isProcessRestoreEnabled = true
        }
        tapDescription = desc
        var tid: AudioObjectID = 0
        let st = AudioHardwareCreateProcessTap(desc, &tid)
        guard st == noErr, tid != 0 else { warn("tap: AudioHardwareCreateProcessTap failed \(osstatusText(st))"); return false }
        tapID = tid
        tappedObjects = objs
        let fmt = getASBD(tid, kAudioTapPropertyFormat)
        var outUIDOpt = defaultOutputUID(system: env["AUDIOTAP_SYSOUT"] != nil)
        if env["AUDIOTAP_MICSUB"] != nil { outUIDOpt = getString(findMicDevice(), kAudioDevicePropertyDeviceUID) }
        guard let outUID = outUIDOpt else { warn("tap: no default output device"); return false }
        let aggUID = "com.voicemode.audiotap." + UUID().uuidString
        var dict: [String: Any] = [
            kAudioAggregateDeviceNameKey: "callwatch tap",
            kAudioAggregateDeviceUIDKey: aggUID,
            kAudioAggregateDeviceMainSubDeviceKey: outUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outUID]],
            kAudioAggregateDeviceTapListKey: [[kAudioSubTapDriftCompensationKey: true,
                                               kAudioSubTapUIDKey: desc.uuid.uuidString]],
        ]
        if env["AUDIOTAP_NOTAPLIST"] != nil { dict.removeValue(forKey: kAudioAggregateDeviceTapListKey) }
        if env["AUDIOTAP_NOSUB"] != nil { dict.removeValue(forKey: kAudioAggregateDeviceSubDeviceListKey); dict.removeValue(forKey: kAudioAggregateDeviceMainSubDeviceKey) }
        var agg: AudioObjectID = 0
        let st2 = AudioHardwareCreateAggregateDevice(dict as CFDictionary, &agg)
        guard st2 == noErr, agg != 0 else { warn("tap: aggregate device failed \(osstatusText(st2))"); return false }
        aggID = agg
        let rate = fmt?.mSampleRate ?? (getFloat64(agg, kAudioDevicePropertyNominalSampleRate) ?? 48000)
        remote.deviceRate = rate
        remote.resampler = Resampler(inRate: rate, outRate: Double(outRate))
        let side = remote
        let diagCounter = Counter()
        let st3 = AudioDeviceCreateIOProcIDWithBlock(&aggProc, agg, ioQueue) { _, inData, _, _, _ in
            let list = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inData))
            if diagCounter.next() < 3 {
                emit(["event": "tap_io", "buffers": list.count,
                      "channels": list.map { Int($0.mNumberChannels) }, "bytes": list.map { Int($0.mDataByteSize) }])
            }
            guard let first = list.first, first.mNumberChannels > 0 else { return }
            let frames = Int(first.mDataByteSize) / (MemoryLayout<Float>.size * Int(first.mNumberChannels))
            side.ingest(inData, frames: frames)
        }
        guard st3 == noErr, let p = aggProc else { warn("tap: IOProc failed \(osstatusText(st3))"); return false }
        let st4 = AudioDeviceStart(agg, p)
        guard st4 == noErr else { warn("tap: start failed \(osstatusText(st4))"); return false }
        let inStreams = getObjectIDs(agg, kAudioDevicePropertyStreams, kAudioObjectPropertyScopeInput)
        let outStreams = getObjectIDs(agg, kAudioDevicePropertyStreams, kAudioObjectPropertyScopeOutput)
        emit(["event": "tap_agg", "agg": Int(agg), "tap": Int(tid), "in_streams": inStreams.count,
              "out_streams": outStreams.count,
              "running": Int(getUInt32(agg, kAudioDevicePropertyDeviceIsRunning) ?? 99),
              "agg_rate": getFloat64(agg, kAudioDevicePropertyNominalSampleRate) ?? 0,
              "tap_fmt_rate": fmt?.mSampleRate ?? 0, "tap_fmt_ch": Int(fmt?.mChannelsPerFrame ?? 0),
              "tap_fmt_id": Int(fmt?.mFormatID ?? 0), "tap_fmt_flags": Int(fmt?.mFormatFlags ?? 0)])
        emit(["event": "tap", "processes": pids.map { Int($0) }, "bundles": bundles,
              "rate": rate, "channels": Int(fmt?.mChannelsPerFrame ?? 0), "output_uid": outUID])
        return true
    }

    // Every 5 s: if the app spawned new audio process objects (Teams helpers do), update the tap.
    func refreshTap() {
        guard tapID != 0, let desc = tapDescription else { return }
        let (objs, bundles, pids) = targetObjects()
        if objs.isEmpty || Set(objs) == Set(tappedObjects) { return }
        desc.processes = objs
        if #available(macOS 26.0, *) { desc.bundleIDs = bundles }
        var addr = address(kAudioTapPropertyDescription)
        var d: CATapDescription = desc
        let size = UInt32(MemoryLayout<CATapDescription>.size)
        let st = withUnsafeMutablePointer(to: &d) { ptr -> OSStatus in
            return AudioObjectSetPropertyData(tapID, &addr, 0, nil, size, ptr)
        }
        if st == noErr {
            tappedObjects = objs
            emit(["event": "tap_update", "processes": pids.map { Int($0) }])
        } else {
            warn("tap: description update failed \(osstatusText(st)) (keeping previous process list)")
            tappedObjects = objs      // do not retry every 5 s
        }
    }

    // ---- writer

    func rotate(_ side: Side) {
        side.writer?.close()
        side.chunkIndex += 1
        let url = outDir.appendingPathComponent(String(format: "%@-%03d.wav", side.name, side.chunkIndex))
        do {
            side.writer = try WavWriter(url: url, rate: outRate)
            emit(["event": "chunk", "side": side.name, "index": side.chunkIndex, "path": url.path])
        } catch {
            warn("\(side.name): cannot open \(url.path): \(error)")
            side.writer = nil
        }
    }

    func rotateMix() {
        mixWriter?.close()
        mixIndex += 1
        let url = outDir.appendingPathComponent(String(format: "mix-%03d.wav", mixIndex))
        do {
            mixWriter = try WavWriter(url: url, rate: outRate)
            emit(["event": "chunk", "side": "mix", "index": mixIndex, "path": url.path])
        } catch {
            warn("mix: cannot open \(url.path): \(error)")
            mixWriter = nil
        }
    }

    func writeSide(_ side: Side, _ samples: [Int16]) {
        var s = samples[...]
        while !s.isEmpty {
            if side.writer == nil || side.writer!.frames >= chunkFrames { rotate(side) }
            guard let w = side.writer else { return }
            let room = chunkFrames - w.frames
            let n = min(room, s.count)
            w.append(Array(s.prefix(n)))
            s = s.dropFirst(n)
        }
    }

    func writeMix(_ samples: [Int16]) {
        var s = samples[...]
        while !s.isEmpty {
            if mixWriter == nil || mixWriter!.frames >= chunkFrames { rotateMix() }
            guard let w = mixWriter else { return }
            let n = min(chunkFrames - w.frames, s.count)
            w.append(Array(s.prefix(n)))
            s = s.dropFirst(n)
        }
    }

    var micOn = false
    var tapOn = false

    func tick(final: Bool = false) {
        let m = micOn ? mic.drain() : []
        var r = tapOn ? remote.drain() : []
        // The tap aggregate only starts once the app produces output, so the
        // remote side can begin seconds after the mic. Pad its first data with
        // silence so both files share one timeline from the recorder start.
        if micOn && tapOn && !r.isEmpty && remote.totalOut == 0 {
            let pad = mic.totalOut + m.count - r.count
            if pad > 0 { r = [Int16](repeating: 0, count: pad) + r }
            emit(["event": "remote_first_audio", "pad_frames": max(pad, 0)])
        }
        if micOn { writeSide(mic, m); mic.totalOut += m.count }
        if tapOn { writeSide(remote, r); remote.totalOut += r.count }
        if micOn && tapOn {
            // Both streams share one timeline (see the padding above), so the mix
            // just adds them sample by sample. If one side stalls for a long time
            // (app output stopped mid-call) the other is not held in memory
            // forever: past 30 s of lead, the stalled side is padded with silence.
            mixPending.mic.append(contentsOf: m)
            mixPending.remote.append(contentsOf: r)
            let stall = 30 * outRate
            if mixPending.mic.count > mixPending.remote.count + stall {
                mixPending.remote.append(contentsOf: [Int16](repeating: 0, count: mixPending.mic.count - mixPending.remote.count - stall / 2))
            } else if mixPending.remote.count > mixPending.mic.count + stall {
                mixPending.mic.append(contentsOf: [Int16](repeating: 0, count: mixPending.remote.count - mixPending.mic.count - stall / 2))
            }
            var n = min(mixPending.mic.count, mixPending.remote.count)
            if final { n = max(mixPending.mic.count, mixPending.remote.count) }
            if n > 0 {
                var mixed = [Int16](repeating: 0, count: n)
                for i in 0..<n {
                    let a = i < mixPending.mic.count ? Int32(mixPending.mic[i]) : 0
                    let b = i < mixPending.remote.count ? Int32(mixPending.remote[i]) : 0
                    mixed[i] = Int16(clamping: a + b)
                }
                writeMix(mixed)
                mixPending.mic.removeFirst(min(n, mixPending.mic.count))
                mixPending.remote.removeFirst(min(n, mixPending.remote.count))
            }
        }
        if final || Date().timeIntervalSince(lastFlush) >= 5 {
            lastFlush = Date()
            mic.writer?.flush(); remote.writer?.flush(); mixWriter?.flush()
        }
    }

    func status() -> [String: Any] {
        return ["event": "status", "elapsed": Int(Date().timeIntervalSince(startedAt)),
                "mic_frames": mic.writer?.frames ?? 0, "remote_frames": remote.writer?.frames ?? 0,
                "mic_callbacks": mic.callbacks, "remote_callbacks": remote.callbacks,
                "mic_chunk": mic.chunkIndex, "remote_chunk": remote.chunkIndex,
                "dropped": mic.dropped + remote.dropped]
    }

    func start() -> Bool {
        try? FileManager.default.createDirectory(at: outDir, withIntermediateDirectories: true)
        if wantMic { micOn = startMic() }
        if wantTap { tapOn = startTap() }
        guard micOn || tapOn else { return false }
        emit(["event": "started", "pid": Int(getpid()), "ppid": Int(getppid()), "mic": micOn, "tap": tapOn, "dir": outDir.path, "rate": outRate])
        let t = DispatchSource.makeTimerSource(queue: writerQueue)
        t.schedule(deadline: .now() + 0.2, repeating: 0.2)
        t.setEventHandler { [weak self] in self?.tick() }
        t.resume(); writerTimer = t
        if tapOn {
            let r = DispatchSource.makeTimerSource(queue: writerQueue)
            r.schedule(deadline: .now() + 5, repeating: 5)
            r.setEventHandler { [weak self] in self?.refreshTap() }
            r.resume(); refreshTimer = r
        }
        if tapOn {
            writerQueue.asyncAfter(deadline: .now() + 1.5) { [weak self] in
                guard let me = self, me.aggID != 0 else { return }
                let subs = getObjectIDs(me.aggID, kAudioAggregateDevicePropertyActiveSubDeviceList)
                emit(["event": "tap_agg_after", "running": Int(getUInt32(me.aggID, kAudioDevicePropertyDeviceIsRunning) ?? 99),
                      "running_somewhere": Int(getUInt32(me.aggID, kAudioDevicePropertyDeviceIsRunningSomewhere) ?? 99),
                      "active_subs": subs.map { Int($0) },
                      "sub_names": subs.map { getString($0, kAudioObjectPropertyName) ?? "?" },
                      "tap_running": Int(getUInt32(me.tapID, kAudioDevicePropertyDeviceIsRunning) ?? 99),
                      "remote_callbacks": me.remote.callbacks])
            }
        }
        let s = DispatchSource.makeTimerSource(queue: writerQueue)
        s.schedule(deadline: .now() + 60, repeating: 60)
        s.setEventHandler { [weak self] in if let me = self { emit(me.status()) } }
        s.resume(); statusTimer = s
        return true
    }
    var statusTimer: DispatchSourceTimer?

    func stop() {
        guard running else { return }
        running = false
        writerTimer?.cancel(); refreshTimer?.cancel(); statusTimer?.cancel()
        if let p = micProc, micDevice != 0 {
            AudioDeviceStop(micDevice, p); AudioDeviceDestroyIOProcID(micDevice, p)
        }
        if let p = aggProc, aggID != 0 {
            AudioDeviceStop(aggID, p); AudioDeviceDestroyIOProcID(aggID, p)
        }
        writerQueue.sync { self.tick(final: true) }
        mic.writer?.close(); remote.writer?.close(); mixWriter?.close()
        if aggID != 0 { AudioHardwareDestroyAggregateDevice(aggID); aggID = 0 }
        if tapID != 0 { AudioHardwareDestroyProcessTap(tapID); tapID = 0 }
        var st = status(); st["event"] = "stopped"
        emit(st)
    }
}

// MARK: - TCC responsibility disclaim (re-spawn so prompts name this binary)

@_silgen_name("responsibility_spawnattrs_setdisclaim")
func responsibility_spawnattrs_setdisclaim(_ attrs: UnsafeMutablePointer<posix_spawnattr_t?>, _ disclaim: Int32) -> Int32

func respawnDisclaimed(args: [String]) -> Never {
    var attr: posix_spawnattr_t? = nil
    posix_spawnattr_init(&attr)
    let drc = responsibility_spawnattrs_setdisclaim(&attr, 1)
    if drc != 0 { warn("disclaim: setdisclaim returned \(drc)") }
    let argv: [UnsafeMutablePointer<CChar>?] = args.map { strdup($0) } + [nil]
    var child: pid_t = 0
    let rc = posix_spawn(&child, args[0], nil, &attr, argv, environ)
    posix_spawnattr_destroy(&attr)
    for p in argv { free(p) }
    if rc != 0 {
        // Could not spawn: run in-process instead (same pid, no disclaim).
        warn("disclaim: posix_spawn failed (\(rc)); continuing in-process")
        let plain: [UnsafeMutablePointer<CChar>?] = (args + ["--no-disclaim"]).map { strdup($0) } + [nil]
        execv(args[0], plain)
        exit(1)
    }
    // Proxy: forward termination signals to the child, exit with its status.
    gChild = child
    let forward: @convention(c) (Int32) -> Void = { sig in kill(gChild, sig) }
    signal(SIGTERM, forward); signal(SIGINT, forward); signal(SIGHUP, forward)
    var status: Int32 = 0
    let myParent = getppid()
    while true {
        let r = waitpid(child, &status, WNOHANG)
        if r == child { break }
        if r < 0 && errno != EINTR { exit(1) }
        if getppid() != myParent { kill(child, SIGTERM) }   // daemon died: take the child down too
        usleep(200_000)
    }
    if (status & 0x7f) == 0 { exit((status >> 8) & 0xff) }
    exit(128 + (status & 0x7f))
}
var gChild: pid_t = 0

// MARK: - main

func usage() -> Never {
    warn("usage: audiotap list | detect | record --out DIR [--app Teams|WhatsApp] [--pid N]... [--no-tap] [--no-mic] [--chunk-seconds N] [--rate N] [--seconds N] [--start-index N] [--mic-device NAME] [--no-disclaim]")
    exit(2)
}

var args = Array(CommandLine.arguments.dropFirst())
guard let cmd = args.first else { usage() }
args.removeFirst()

switch cmd {
case "list":
    printJSON(processObjects().map { $0.json })
case "detect":
    let procs = processObjects().filter { $0.app != nil }
    let active = procs.filter { $0.runningInput }
    var byApp: [String: Any] = [:]
    for a in ["Teams", "WhatsApp"] {
        let mine = procs.filter { $0.app == a }
        byApp[a] = ["present": !mine.isEmpty,
                    "input": mine.contains { $0.runningInput },
                    "output": mine.contains { $0.runningOutput },
                    "pids": mine.map { Int($0.pid) },
                    "input_pids": mine.filter { $0.runningInput }.map { Int($0.pid) }]
    }
    printJSON(["active": active.map { $0.app! }.sorted(), "apps": byApp, "processes": procs.map { $0.json }])
case "record":
    var out: String? = nil
    var app: String? = nil
    var pids: [pid_t] = []
    var tap = true, mic = true, disclaim = true
    var chunk = 600, rate = 16000, seconds = 0, startIndex = 1
    var micDev = ""
    var i = 0
    func next() -> String { i += 1; guard i < args.count else { usage() }; return args[i] }
    while i < args.count {
        switch args[i] {
        case "--out": out = next()
        case "--app": app = next()
        case "--pid": pids.append(pid_t(next()) ?? 0)
        case "--no-tap": tap = false
        case "--no-mic": mic = false
        case "--no-disclaim": disclaim = false
        case "--chunk-seconds": chunk = Int(next()) ?? 600
        case "--rate": rate = Int(next()) ?? 16000
        case "--seconds": seconds = Int(next()) ?? 0
        case "--start-index": startIndex = Int(next()) ?? 1
        case "--mic-device": micDev = next()
        default: warn("unknown option \(args[i])"); usage()
        }
        i += 1
    }
    guard let outDir = out else { usage() }
    if disclaim {
        respawnDisclaimed(args: [CommandLine.arguments[0], "record"] + args + ["--no-disclaim"])
    }
    let rec = Recorder(outDir: URL(fileURLWithPath: outDir), outRate: rate, chunkSeconds: chunk, app: app,
                       pids: pids.filter { $0 > 0 }, tap: tap, mic: mic, startIndex: startIndex, micMatch: micDev)
    guard rec.start() else { warn("record: nothing could be started"); exit(1) }
    signal(SIGTERM, SIG_IGN); signal(SIGINT, SIG_IGN); signal(SIGHUP, SIG_IGN)
    let stopQueue = DispatchQueue(label: "audiotap.stop")
    var stopping = false
    func requestStop() {
        stopQueue.async {
            if stopping { return }
            stopping = true
            rec.stop()
            exit(0)
        }
    }
    let sources = [SIGTERM, SIGINT, SIGHUP].map { sig -> DispatchSourceSignal in
        let s = DispatchSource.makeSignalSource(signal: sig, queue: stopQueue)
        s.setEventHandler { requestStop() }
        s.resume()
        return s
    }
    _ = sources
    if seconds > 0 {
        DispatchQueue.global().asyncAfter(deadline: .now() + .seconds(seconds)) { requestStop() }
    }
    // Parent (the python daemon) died: stop too, do not run on as an orphan.
    let ppid = getppid()
    let orphan = DispatchSource.makeTimerSource(queue: stopQueue)
    orphan.schedule(deadline: .now() + 2, repeating: 2)
    orphan.setEventHandler { if getppid() != ppid { warn("record: parent went away, stopping"); requestStop() } }
    orphan.resume()
    dispatchMain()
default:
    usage()
}
