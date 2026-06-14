; Art installation - 8-motor oscillation
; Units: degrees (360 = 1 full motor rotation)
; Amplitude: adjust the target positions below once spindle diameter is known
;   rope_travel_mm = amplitude_deg / 360 * pi * spindle_diameter_mm
; Feedrate: deg/min (720 = 2 rot/min, 360 = 1 rot/min)

M203 X36000 Y36000 Z36000 A36000 B36000 C36000 U36000 V36000
M204 S3600

G92 X0 Y0 Z0 A0 B0 C0 U0 V0   ; declare boot position as zero, no homing needed
G90                              ; absolute positioning

M808 K0                          ; loop start — repeat forever

G1 X360 Y360 Z360 A360 B360 C360 U360 V360 F18000  ; all motors forward 1 rotation
G4 P500                                              ; pause 0.5 s at end
G1 X0 Y0 Z0 A0 B0 C0 U0 V0 F18000                  ; all motors back to start
G4 P500                                              ; pause 0.5 s at end

M808                             ; jump back to loop start
