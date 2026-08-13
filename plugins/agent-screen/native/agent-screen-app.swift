// agent-screen-app.swift — "Agent Screen": virtuelles Display + Live-Fenster + MJPEG-Stream.
// Fork-Kern aus DeskPad (MIT, Stengo/DeskPad): CGVirtualDisplay private API + CGDisplayStream.
// Ersetzt DeskPad + agent-screen-stream.py + agent-screen-viewer in EINEM Prozess.
//
// Build: swiftc -O agent-screen-app.swift -import-objc-header CGVirtualDisplayPrivate.h -o agent-screen-app
// Danach: codesign -s - agent-screen-app   (TCC-Grant für Bildschirmaufnahme einmalig nötig)

import Cocoa
import CoreImage
import ImageIO
import Network
import UniformTypeIdentifiers
import ApplicationServices

// MARK: - MJPEG-Server (Loopback, fürs Hermes-Plugin-Pane)

final class MJpegServer {
    private var listener: NWListener?
    private var clients: [NWConnection] = []
    private let queue = DispatchQueue(label: "mjpeg.server")
    private let port: NWEndpoint.Port

    init(port: NWEndpoint.Port = 8788) { self.port = port }

    func start() throws {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        params.requiredInterfaceType = .loopback
        let listener = try NWListener(using: params, on: port)
        listener.newConnectionHandler = { [weak self] conn in
            guard let self else { return }
            self.queue.async { self.handle(conn) }
        }
        listener.start(queue: queue)
        self.listener = listener
    }

    private func handle(_ conn: NWConnection) {
        conn.start(queue: queue)
        conn.receive(minimumIncompleteLength: 1, maximumLength: 8192) { [weak self] data, _, _, _ in
            guard let self, let data, let req = String(data: data, encoding: .utf8) else { return }
            if req.hasPrefix("GET /ping") {
                conn.send(content: Data("HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok".utf8),
                          completion: .contentProcessed { _ in conn.cancel() })
            } else {
                let head = "HTTP/1.1 200 OK\r\nContent-Type: multipart/x-mixed-replace; boundary=frame\r\nCache-Control: no-cache\r\nConnection: keep-alive\r\n\r\n"
                conn.send(content: head.data(using: .utf8)!, completion: .contentProcessed { _ in
                    self.queue.async { self.clients.append(conn) }
                })
            }
        }
    }

    func broadcast(_ jpeg: Data) {
        queue.async {
            let frame = Data("--frame\r\nContent-Type: image/jpeg\r\nContent-Length: \(jpeg.count)\r\n\r\n".utf8) + jpeg + Data("\r\n".utf8)
            for conn in self.clients {
                conn.send(content: frame, completion: .contentProcessed { _ in })
            }
            self.clients.removeAll { conn in
                if case .failed = conn.state { return true }
                return conn.state == .cancelled
            }
        }
    }
}

// MARK: - AppDelegate: Fenster + virtuelles Display + Stream

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var window: NSWindow!
    private var contentView: NSView!
    private var display: CGVirtualDisplay!
    private var stream: CGDisplayStream?
    private var server = MJpegServer()
    private let ciContext = CIContext()
    private var frameCounter = 0

    // Drag-Portal: welches fremde Fenster wird gerade gezogen?
    private var dragCandidateWindowID: CGWindowID = 0
    private var dragCandidatePID: pid_t = 0
    private var dragCandidateBounds: CGRect = .zero
    private var dragWasPressed = false
    private var dragSeenMovement = false
    private var dragWatchTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        // --- Fenster ---
        let rect = NSRect(x: 0, y: 0, width: 960, height: 600)
        window = NSWindow(contentRect: rect,
                          styleMask: [.titled, .closable, .miniaturizable, .resizable],
                          backing: .buffered, defer: false)
        window.title = "Agent Screen"
        window.minSize = NSSize(width: 200, height: 125)
        // DeskPad-Setup: transparente Titlebar, damit backgroundColor sie mitfärbt
        window.titlebarAppearsTransparent = true
        window.isMovableByWindowBackground = true
        window.backgroundColor = .windowBackgroundColor
        contentView = NSView(frame: rect)
        contentView.wantsLayer = true
        contentView.layer?.backgroundColor = NSColor.black.cgColor
        window.contentView = contentView
        window.center()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        // Klick ins Fenster → Cursor aufs virtuelle Display (wie DeskPad)
        contentView.addGestureRecognizer(NSClickGestureRecognizer(target: self, action: #selector(didClickOnScreen)))

        // --- MJPEG-Server ---
        do {
            try server.start()
            NSLog("[agent-screen] MJPEG auf http://127.0.0.1:8788/stream.mjpeg")
        } catch {
            NSLog("[agent-screen] Server-Fehler: \(error)")
        }

        // --- Virtuelles Display erzeugen (DeskPad-Kern, private API) ---
        let descriptor = CGVirtualDisplayDescriptor()
        descriptor.setDispatchQueue(DispatchQueue.main)
        descriptor.name = "Agent Screen Display"
        descriptor.maxPixelsWide = 5120
        descriptor.maxPixelsHigh = 2160
        descriptor.sizeInMillimeters = CGSize(width: 1600, height: 1000)
        descriptor.productID = 0x1234
        descriptor.vendorID = 0x3456
        descriptor.serialNum = 0x0001

        let display = CGVirtualDisplay(descriptor: descriptor)
        self.display = display

        let settings = CGVirtualDisplaySettings()
        settings.hiDPI = 1
        settings.modes = [
            CGVirtualDisplayMode(width: 3360, height: 2100, refreshRate: 60),
            CGVirtualDisplayMode(width: 3840, height: 2160, refreshRate: 60),
            CGVirtualDisplayMode(width: 2560, height: 1440, refreshRate: 60),
            CGVirtualDisplayMode(width: 1920, height: 1080, refreshRate: 60),
            CGVirtualDisplayMode(width: 1600, height: 900, refreshRate: 60),
            CGVirtualDisplayMode(width: 1280, height: 720, refreshRate: 60),
        ]
        display.apply(settings)
        NSLog("[agent-screen] Display erzeugt: ID \(display.displayID)")

        // --- Display-Inhalt streamen (CGDisplayStream, IOSurface-Frames) ---
        let stream = CGDisplayStream(
            dispatchQueueDisplay: display.displayID,
            outputWidth: 3360,
            outputHeight: 2100,
            pixelFormat: 1_111_970_369, // kCVPixelFormatType_32BGRA
            properties: [CGDisplayStream.showCursor: true] as CFDictionary,
            queue: .main,
            handler: { [weak self] _, _, frameSurface, _ in
                self?.handleFrame(surface: frameSurface)
            }
        )
        self.stream = stream
        stream?.start()
        NSLog("[agent-screen] CGDisplayStream läuft.")

        // Seitenverhältnis des Fensters an die Display-Auflösung koppeln
        window.contentAspectRatio = NSSize(width: 3360, height: 2100)

        // Titlebar-Feedback: grünlich, solange der Cursor auf dem Agent-Screen ist
        Timer.scheduledTimer(withTimeInterval: 0.25, repeats: true) { [weak self] _ in
            self?.updateTitlebarHighlight()
        }

        // Drag-Portal: fremde Fenster aufs virtuelle Display ziehen
        installWindowDragMonitor()
    }

    // MARK: - Drag-Portal (Fenster auf den Agent Screen ziehen)

    /// Erkennt, wenn der User ein fremdes App-Fenster packt und über unserem
    /// Fenster loslässt → das Fenster wird auf die Mitte des virtuellen Displays
    /// teleportiert (wie bei DeskPad, aber intuitiver: Drop aufs Fenster).
    ///
    /// Technik: Polling statt Event-Monitor. Bei Titelleisten-Drags übernimmt
    /// der WindowServer die Maus-Events — globale Monitore sehen sie nicht
    /// (besonders bei synthetischen Events). Stattdessen: Jeder Tick prüft
    /// (a) linke Maustaste gedrückt? (b) bewegt sich ein fremdes Fenster?
    /// (c) Cursor über unserem Fenster? Beim Loslassen über unserem Fenster
    /// mit vorheriger Bewegung → teleportieren.
    private func installWindowDragMonitor() {
        let timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            self?.tickDragWatch()
        }
        dragWatchTimer = timer
        NSLog("[agent-screen] Drag-Portal aktiv: Fenster aufs virtuelle Display ziehbar")
    }

    private func tickDragWatch() {
        let pressed = (NSEvent.pressedMouseButtons & 1) != 0
        let mouse = NSEvent.mouseLocation
        let overOurWindow = window.frame.contains(mouse)
        // WICHTIG: CGWindowBounds, NSEvent.mouseLocation und AppleScript
        // position nutzen ALLE den globalen Quartz-Raum (Ursprung unten-links)
        // — KEINE Umrechnung. Nur Toleranz beim Hit-Test.

        if pressed {
            if !dragWasPressed {
                // Frischer Maus-Down: Kandidat merken (fremdes Fenster unter Cursor)
                if let info = windowInfo(at: mouse),
                   (info[kCGWindowOwnerName as String] as? String) != "Agent Screen",
                   (info[kCGWindowLayer as String] as? Int) == 0 {
                    dragCandidateWindowID = info[kCGWindowNumber as String] as? CGWindowID ?? 0
                    dragCandidatePID = info[kCGWindowOwnerPID as String] as? pid_t ?? 0
                    if let boundsDict = info[kCGWindowBounds as String] as? [String: CGFloat] {
                        dragCandidateBounds = CGRect(x: boundsDict["X"] ?? 0, y: boundsDict["Y"] ?? 0,
                                                     width: boundsDict["Width"] ?? 0, height: boundsDict["Height"] ?? 0)
                    }
                    dragSeenMovement = false
                    NSLog("[agent-screen] Drag-Portal: Kandidat wID=%d pid=%d", dragCandidateWindowID, dragCandidatePID)
                } else {
                    dragCandidateWindowID = 0
                    dragCandidatePID = 0
                    // still — kein Kandidat ist normal (Klick auf Desktop etc.)
                }
            } else if dragCandidateWindowID != 0, !dragSeenMovement {
                // Bewegt sich das Kandidaten-Fenster gerade? (Titelleisten-Drag)
                if let info = windowInfoByID(dragCandidateWindowID),
                   let boundsDict = info[kCGWindowBounds as String] as? [String: CGFloat] {
                    let now = CGRect(x: boundsDict["X"] ?? 0, y: boundsDict["Y"] ?? 0,
                                     width: boundsDict["Width"] ?? 0, height: boundsDict["Height"] ?? 0)
                    if abs(now.minX - dragCandidateBounds.minX) > 4 ||
                       abs(now.minY - dragCandidateBounds.minY) > 4 {
                        dragSeenMovement = true
                    }
                }
            }
        } else {
            // Losgelassen: War ein Drag über unserem Fenster? → teleportieren
            if dragWasPressed, dragSeenMovement, overOurWindow,
               dragCandidateWindowID != 0, dragCandidatePID != 0 {
                moveWindowToAgentScreen(pid: dragCandidatePID, windowID: dragCandidateWindowID)
            }
            dragCandidateWindowID = 0
            dragCandidatePID = 0
            dragSeenMovement = false
        }
        dragWasPressed = pressed
    }

    /// Oberstes NORMALES Fenster (Layer 0) unter einem Punkt finden.
    /// WICHTIG: CGWindowList enthält auch Dock/Menüleiste (Layer 20+); das
    /// erste Fenster in der Liste ist NICHT das oberste App-Fenster. Daher
    /// alle Treffer sammeln und das mit dem höchsten Layer ≤ 0 nehmen.
    private func windowInfo(at point: CGPoint) -> [String: Any]? {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
                as? [[String: Any]] else { return nil }
        let tol: CGFloat = 5 // Toleranz: contains() ist an maxX/maxY exklusiv
        var best: [String: Any]?
        var bestLayer = Int.min
        for info in list {
            guard let boundsDict = info[kCGWindowBounds as String] as? [String: CGFloat],
                  let alpha = info[kCGWindowAlpha as String] as? CGFloat, alpha > 0,
                  let layer = info[kCGWindowLayer as String] as? Int, layer <= 0 else { continue }
            let bounds = CGRect(x: boundsDict["X"] ?? 0, y: boundsDict["Y"] ?? 0,
                                width: boundsDict["Width"] ?? 0, height: boundsDict["Height"] ?? 0)
                .insetBy(dx: -tol, dy: -tol)
            if bounds.contains(point), layer > bestLayer {
                best = info
                bestLayer = layer
            }
        }
        return best
    }

    private func windowInfoByID(_ id: CGWindowID) -> [String: Any]? {
        guard let list = CGWindowListCopyWindowInfo([.optionOnScreenOnly, .excludeDesktopElements], kCGNullWindowID)
                as? [[String: Any]] else { return nil }
        return list.first { ($0[kCGWindowNumber as String] as? CGWindowID) == id }
    }

    /// Das gezogene Fenster per Accessibility-API auf die Mitte des virtuellen
    /// Displays verschieben (CGDisplayMoveCursorToPoint wäre nur Cursor).
    private func moveWindowToAgentScreen(pid: pid_t, windowID: CGWindowID) {
        guard let screen = NSScreen.screens.first(where: { $0.localizedName.contains("Agent Screen") }) else {
            NSLog("[agent-screen] Drag-Portal: Agent-Screen-Display nicht gefunden")
            return
        }
        let appRef = AXUIElementCreateApplication(pid)
        var windowsRef: CFTypeRef?
        let err = AXUIElementCopyAttributeValue(appRef, kAXWindowsAttribute as CFString, &windowsRef)
        guard err == .success, let windows = windowsRef as? [AXUIElement] else {
            NSLog("[agent-screen] Drag-Portal: AX-Fensterliste fehlgeschlagen (%@)", String(describing: err))
            return
        }
        // AKTUELLE Bounds des gezogenen Fensters holen (es hat sich während
        // des Drags bewegt — die Bounds vom Maus-Down sind veraltet!)
        var matchBounds = dragCandidateBounds
        if let info = windowInfoByID(windowID),
           let boundsDict = info[kCGWindowBounds as String] as? [String: CGFloat] {
            matchBounds = CGRect(x: boundsDict["X"] ?? 0, y: boundsDict["Y"] ?? 0,
                                 width: boundsDict["Width"] ?? 0, height: boundsDict["Height"] ?? 0)
        }
        // Das gezogene Fenster über seine Bounds identifizieren (Position+Größe
        // zum Zeitpunkt des Loslassens; Toleranz für minimale Abweichungen).
        let tolerance: CGFloat = 60
        var target: AXUIElement?
        for window in windows {
            var posRef: CFTypeRef?
            var sizeRef: CFTypeRef?
            guard AXUIElementCopyAttributeValue(window, kAXPositionAttribute as CFString, &posRef) == .success,
                  AXUIElementCopyAttributeValue(window, kAXSizeAttribute as CFString, &sizeRef) == .success,
                  let posValue = posRef,
                  let sizeValue = sizeRef else { continue }
            var pos = CGPoint.zero
            var size = CGSize.zero
            AXValueGetValue(posValue as! AXValue, .cgPoint, &pos)
            AXValueGetValue(sizeValue as! AXValue, .cgSize, &size)
            let bounds = CGRect(origin: pos, size: size)
            if abs(bounds.minX - matchBounds.minX) < tolerance,
               abs(bounds.minY - matchBounds.minY) < tolerance,
               abs(bounds.width - matchBounds.width) < tolerance,
               abs(bounds.height - matchBounds.height) < tolerance {
                target = window
                break
            }
        }
        guard let window = target else {
            NSLog("[agent-screen] Drag-Portal: Fenster %d nicht in AX-Liste (PID %d)", windowID, pid)
            return
        }
        // Auf die Display-Mitte setzen (Fenster bleibt zentriert)
        let center = NSPoint(x: screen.frame.midX, y: screen.frame.midY)
        var size = CGSize(width: 800, height: 500)
        var sizeRef: CFTypeRef?
        if AXUIElementCopyAttributeValue(window, kAXSizeAttribute as CFString, &sizeRef) == .success, let sizeValue = sizeRef {
            AXValueGetValue(sizeValue as! AXValue, .cgSize, &size)
        }
        var newOrigin = CGPoint(x: center.x - size.width / 2, y: center.y - size.height / 2)
        if let posValue = AXValueCreate(.cgPoint, &newOrigin) {
            AXUIElementSetAttributeValue(window, kAXPositionAttribute as CFString, posValue)
            NSLog("[agent-screen] Drag-Portal: Fenster %d → Agent Screen @ %@", windowID, NSStringFromPoint(newOrigin))
        }
    }

    private func updateTitlebarHighlight() {
        let mouse = NSEvent.mouseLocation
        let onAgentScreen = NSScreen.screens.contains {
            $0.localizedName.contains("Agent Screen") && NSMouseInRect(mouse, $0.frame, false)
        }
        let target: NSColor = onAgentScreen
            ? NSColor(calibratedRed: 0.086, green: 0.639, blue: 0.290, alpha: 1.0) // Teknium-Grün #16A34A
            : NSColor.windowBackgroundColor
        if window.backgroundColor != target {
            window.backgroundColor = target
            NSLog("[agent-screen] Titlebar → \(onAgentScreen ? "BLAU" : "neutral") (Maus \(Int(mouse.x)),\(Int(mouse.y)))")
        }
    }

    private func handleFrame(surface: IOSurface?) {
        guard let surface else { return }
        // Fenster direkt aus der IOSurface rendern (wie DeskPad)
        contentView.layer?.contents = surface

        // MJPEG: jeden 2. Frame (~2-3 fps) als JPEG encodieren (Hintergrund-Queue)
        frameCounter += 1
        guard frameCounter % 2 == 0 else { return }
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let ci = CIImage(ioSurface: surface)
            let targetW = 1280
            let scale = Double(targetW) / Double(ci.extent.width)
            let targetH = max(1, Int(Double(ci.extent.height) * scale))
            guard let ctx = CGContext(data: nil, width: targetW, height: targetH,
                                      bitsPerComponent: 8, bytesPerRow: 0,
                                      space: CGColorSpaceCreateDeviceRGB(),
                                      bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue),
                  let cg = self.ciContext.createCGImage(ci, from: ci.extent),
                  ctx.draw(cg, in: CGRect(x: 0, y: 0, width: targetW, height: targetH)) as Void? != nil,
                  let scaled = ctx.makeImage(),
                  let data = CFDataCreateMutable(nil, 0),
                  let dest = CGImageDestinationCreateWithData(data, UTType.jpeg.identifier as CFString, 1, nil)
            else { return }
            CGImageDestinationAddImage(dest, scaled, [kCGImageDestinationLossyCompressionQuality: 0.55] as CFDictionary)
            if CGImageDestinationFinalize(dest) {
                self.server.broadcast(data as Data)
            }
        }
    }

    // Klick im Fenster → Cursor auf den Punkt des virtuellen Displays warpen.
    // Der User kann dann direkt auf dem Agent-Screen klicken/tippen (wie DeskPad).
    @objc private func didClickOnScreen(_ gesture: NSGestureRecognizer) {
        let p = gesture.location(in: contentView)
        let w = contentView.bounds.width
        let h = contentView.bounds.height
        guard w > 0, h > 0, display != nil else { return }
        let dispW: CGFloat = 3360
        let dispH: CGFloat = 2100
        let x = p.x / w * dispW
        let y = (h - p.y) / h * dispH
        CGDisplayMoveCursorToPoint(display.displayID, CGPoint(x: x, y: y))
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        return true
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
