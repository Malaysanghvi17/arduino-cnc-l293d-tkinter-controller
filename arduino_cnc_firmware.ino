#include <Stepper.h>
#include <Servo.h>

const int STEPS_PER_REV = 200;
const int MOTOR_RPM     = 50;
const int PEN_SERVO_PIN = 13;

// Calibration defaults — overridden at runtime via PCAL command
int PEN_UP_ANGLE   = 90;
int PEN_DOWN_ANGLE = 30;
int currentRPM     = 30;

// ── Track the ACTUAL last-written servo angle so interpolation never jumps ──
// Initialised to PEN_DOWN_ANGLE so the startup ramp always moves toward UP.
int currentServoAngle = 30;   // matches PEN_DOWN_ANGLE default

Stepper motorX(STEPS_PER_REV, 5, 6, 7, 8);
Stepper motorY(STEPS_PER_REV, 9, 10, 11, 12);
Servo   penServo;

long currentX   = 0;
long currentY   = 0;
bool penIsDown  = false;


// ── Actuator helpers ─────────────────────────────────────────────────────────
void releaseX() { digitalWrite(5,LOW); digitalWrite(6,LOW); digitalWrite(7,LOW); digitalWrite(8,LOW); }
void releaseY() { digitalWrite(9,LOW); digitalWrite(10,LOW); digitalWrite(11,LOW); digitalWrite(12,LOW); }

void emergencyStop() {
  releaseX(); releaseY();
  if (penServo.attached()) penServo.detach();
  for (int p = 5; p <= 12; p++) digitalWrite(p, LOW);
  digitalWrite(PEN_SERVO_PIN, LOW);
}

// ── Slow servo move using tracked currentServoAngle as real start ─────────────
void slowServoMove(int toAngle) {
  int fromAngle = currentServoAngle;
  toAngle = constrain(toAngle, 0, 180);
  if (!penServo.attached()) penServo.attach(PEN_SERVO_PIN);
  int steps = abs(toAngle - fromAngle);
  if (steps > 0) {
    int dir = (toAngle > fromAngle) ? 1 : -1;
    for (int i = 0; i <= steps; i++) {
      penServo.write(fromAngle + i * dir);
      delay(10);          // 10 ms/degree — smooth & safe
    }
  } else {
    penServo.write(toAngle);
    delay(100);
  }
  currentServoAngle = toAngle;
  delay(80);
  penServo.detach();      // release coil current; gears hold position
}

void setPen(bool down) {
  int endAngle = down ? PEN_DOWN_ANGLE : PEN_UP_ANGLE;
  penIsDown = down;
  slowServoMove(endAngle);
}

void stepMotorExact(Stepper &motor, int dir) { motor.step(dir); }

void moveX(long totalSteps) {
  if (!totalSteps) return;
  const int dir = (totalSteps > 0) ? 1 : -1;
  for (long i = 0; i < labs(totalSteps); ++i) {
    if (i < 5) delay(10);
    stepMotorExact(motorX, dir);
    currentX += dir;
  }
  releaseX();
}

void moveY(long totalSteps) {
  if (!totalSteps) return;
  const int dir = (totalSteps > 0) ? 1 : -1;
  for (long i = 0; i < labs(totalSteps); ++i) {
    if (i < 5) delay(10);
    stepMotorExact(motorY, dir);
    currentY += dir;
  }
  releaseY();
}

void moveTo(long stepsX, long stepsY) {
  const long dx = labs(stepsX), dy = labs(stepsY);
  const int  sx = (stepsX >= 0) ? 1 : -1, sy = (stepsY >= 0) ? 1 : -1;
  long remX = dx, remY = dy, err = dx - dy;
  long stepCount = 0;
  while (remX > 0 || remY > 0) {
    const long e2 = err * 2L;
    if (stepCount < 5) delay(10);
    if (remX > 0 && e2 > -dy) { err -= dy; stepMotorExact(motorX, sx); currentX += sx; remX--; }
    if (remY > 0 && e2 <  dx) { err += dx; stepMotorExact(motorY, sy); currentY += sy; remY--; }
    stepCount++;
  }
  releaseX(); releaseY();
}

void goHome() {
  if (penIsDown) setPen(false);
  moveTo(-currentX, -currentY);
}

// ── Shape helpers ─────────────────────────────────────────────────────────────
void drawSquare(long size) {
  goHome(); setPen(true);
  moveTo(size,0); moveTo(0,size); moveTo(-size,0); moveTo(0,-size);
  setPen(false); goHome();
}
void drawRectangle(long w, long h) {
  goHome(); setPen(true);
  moveTo(w,0); moveTo(0,h); moveTo(-w,0); moveTo(0,-h);
  setPen(false); goHome();
}
void drawTriangle(long size) {
  goHome(); setPen(true);
  moveTo(size,0); moveTo(-(size/2),size); moveTo(-(size/2),-size);
  setPen(false); goHome();
}
void drawDiamond(long size) {
  goHome(); setPen(true);
  moveTo(size,size); moveTo(size,-size); moveTo(-size,-size); moveTo(-size,size);
  setPen(false); goHome();
}
void drawZigzag(long size, int repeats) {
  goHome(); setPen(true);
  for (int i = 0; i < repeats; i++) { moveTo(size,size); moveTo(size,-size); }
  setPen(false); goHome();
}
void drawSpiral(long startSize, int rings) {
  goHome(); setPen(true);
  for (int i = 1; i <= rings; i++) {
    long s = startSize * i;
    moveTo(s,0); moveTo(0,s); moveTo(-s,0); moveTo(0,-s);
  }
  setPen(false); goHome();
}

// ── Serial output ─────────────────────────────────────────────────────────────
void printOk() {
  Serial.print("OK X="); Serial.print(currentX);
  Serial.print(" Y=");   Serial.print(currentY);
  Serial.print(" PEN="); Serial.println(penIsDown ? "DOWN" : "UP");
}
void printError(const char *msg) { Serial.print("ERR "); Serial.println(msg); }

void printHelp() {
  Serial.println("CNC ready. Commands:");
  Serial.println("  X<n>        move X axis");
  Serial.println("  Y<n>        move Y axis");
  Serial.println("  M<x,y>      diagonal move");
  Serial.println("  HOME        return to origin (pen up)");
  Serial.println("  ZERO        treat current location as origin");
  Serial.println("  STATUS      report current state");
  Serial.println("  PU / PD     pen up / pen down");
  Serial.println("  PCAL U<n>   set pen-UP angle (0-180)");
  Serial.println("  PCAL D<n>   set pen-DOWN angle (0-180)");
  Serial.println("  PCAL N<n>   nudge servo by n degrees (signed)");
  Serial.println("  PCAL?       query current pen angles");
  Serial.println("  S<n>        set motor speed (RPM)");
  Serial.println("  STOP        emergency stop / cut power");
}

bool parseLongPair(const String &cmd, int start, long &a, long &b) {
  const int comma = cmd.indexOf(',', start);
  if (comma <= start) return false;
  a = cmd.substring(start, comma).toInt();
  b = cmd.substring(comma + 1).toInt();
  return true;
}

// ── Command parser ────────────────────────────────────────────────────────────
void parseCommand(String cmd) {
  cmd.trim(); cmd.replace("\r",""); cmd.replace("\n",""); cmd.trim();
  if (cmd.length() == 0) return;
  String upper = cmd; upper.toUpperCase();

  // ── Pen calibration ──────────────────────────────────────────────────────
  if (upper.startsWith("PCAL")) {
    String sub = upper.substring(4); sub.trim();
    if (sub == "?") {
      Serial.print("PEN_UP=");   Serial.print(PEN_UP_ANGLE);
      Serial.print(" PEN_DOWN="); Serial.println(PEN_DOWN_ANGLE);
      printOk(); return;
    }
    if (sub.length() >= 2) {
      char mode = sub[0];
      int  val  = sub.substring(1).toInt();

      if (mode == 'U') {
        PEN_UP_ANGLE = constrain(val, 0, 180);
        // Only actuate if pen is currently up — use slow ramp
        if (!penIsDown) {
          slowServoMove(PEN_UP_ANGLE);
        }
        Serial.print("PEN_UP="); Serial.println(PEN_UP_ANGLE);
        printOk(); return;
      }
      if (mode == 'D') {
        PEN_DOWN_ANGLE = constrain(val, 0, 180);
        // Only actuate if pen is currently down — use slow ramp
        if (penIsDown) {
          slowServoMove(PEN_DOWN_ANGLE);
        }
        Serial.print("PEN_DOWN="); Serial.println(PEN_DOWN_ANGLE);
        printOk(); return;
      }
      if (mode == 'N') {
        int target = constrain(currentServoAngle + val, 0, 180);
        // Update the appropriate stored angle
        if (penIsDown) PEN_DOWN_ANGLE = target;
        else           PEN_UP_ANGLE   = target;
        slowServoMove(target);
        Serial.print("NUDGE=");    Serial.print(target);
        Serial.print(" PEN_UP=");  Serial.print(PEN_UP_ANGLE);
        Serial.print(" PEN_DOWN="); Serial.println(PEN_DOWN_ANGLE);
        printOk(); return;
      }
    }
    printError("Bad PCAL command. Use PCAL U<n>, PCAL D<n>, PCAL N<n>, PCAL?");
    return;
  }

  // ── Motion & pen commands ─────────────────────────────────────────────────
  bool handled = true;
  if      (upper[0]=='X' && upper.length()>1)  { moveX(upper.substring(1).toInt()); }
  else if (upper[0]=='Y' && upper.length()>1)  { moveY(upper.substring(1).toInt()); }
  else if (upper[0]=='M') {
    long sx=0, sy=0;
    if (!parseLongPair(upper,1,sx,sy)) { printError("Bad M command"); return; }
    moveTo(sx,sy);
  }
  else if (upper=="HOME")   { goHome(); }
  else if (upper=="ZERO")   { currentX=0; currentY=0; }
  else if (upper=="STATUS") { /* fall through to printOk */ }
  else if (upper=="PU" || upper=="PENUP")   { setPen(false); }
  else if (upper=="PD" || upper=="PENDOWN") { setPen(true);  }
  else if (upper[0]=='S' && upper.length()>1) {
    currentRPM = constrain(upper.substring(1).toInt(), 1, 100);
    motorX.setSpeed(currentRPM); motorY.setSpeed(currentRPM);
    Serial.print("SPEED="); Serial.println(currentRPM);
  }
  else if (upper=="STOP") { emergencyStop(); Serial.println("STOPPED"); }
  else if (upper.startsWith("SQ"))  { drawSquare(upper.substring(2).toInt()); }
  else if (upper.startsWith("TR"))  { drawTriangle(upper.substring(2).toInt()); }
  else if (upper.startsWith("DM"))  { drawDiamond(upper.substring(2).toInt()); }
  else if (upper.startsWith("ZZ"))  { drawZigzag(upper.substring(2).toInt(), 4); }
  else if (upper.startsWith("SP"))  {
    long startSize=0, rings=0;
    if (!parseLongPair(upper,2,startSize,rings)) { printError("Bad SP command"); return; }
    drawSpiral(startSize,(int)rings);
  }
  else if (upper.startsWith("RC"))  {
    long w=0, h=0;
    if (!parseLongPair(upper,2,w,h)) { printError("Bad RC command"); return; }
    drawRectangle(w,h);
  }
  else { handled = false; }

  if (!handled) { printError("Unknown command"); return; }
  printOk();
}

void setup() {
  for (int p = 5; p <= 12; p++) pinMode(p, OUTPUT);
  motorX.setSpeed(currentRPM);
  motorY.setSpeed(currentRPM);

  // Do NOT move servo on startup — avoid damaging the tool/bed.
  // Just record the assumed angle without attaching/writing.
  // The servo will only move on the first explicit PU/PD/PCAL command.
  currentServoAngle = PEN_UP_ANGLE;
  penIsDown = false;
  // Do NOT attach the servo here — leave it unpowered until first command.

  Serial.begin(9600);
  while (Serial.available()) Serial.read();   // flush boot noise
  printHelp();
  printOk();
}

void loop() {
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    parseCommand(cmd);
  }
}
