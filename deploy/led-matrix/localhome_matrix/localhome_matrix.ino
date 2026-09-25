// LocalHome's status on the Arduino UNO Q's 13x8 LED matrix.
//
// This sketch only draws. What to show is decided on the Linux side, in
// LocalHome (src/localhome/services/led_matrix.py), which sends a whole
// frame a couple of times a second through arduino-router: 104 characters,
// one per pixel, row by row from the top left, each '0' (off) to '7' (full
// brightness).
//
// The one thing this side decides on its own is what the Linux side can't
// say about itself:
//   - since power-up, nothing heard yet  -> a dot running along the bottom
//   - frames stopped for SILENCE_MS      -> a slowly blinking X: LocalHome
//                                           has stopped, crashed or hung
//
// Build and load it from the board itself - see
// docs/quick-installation-arduino-uno-q.md, "The LED matrix":
//   arduino-cli compile --upload -b arduino:zephyr:unoq deploy/led-matrix/localhome_matrix

#include <Arduino_RouterBridge.h>
#include "Arduino_LED_Matrix.h"

Arduino_LED_Matrix matrix;

static const int W = 13;
static const int H = 8;
static const int N = W * H;
// LocalHome sends a frame every half second; a restart takes a few
// seconds. Twenty seconds without one is not a restart.
static const unsigned long SILENCE_MS = 20000;

uint8_t pixels[N];
unsigned long lastFrameMs = 0;
bool everHeard = false;

// Called from the loop thread (provide_safe), never alongside loop().
bool lhFrame(MsgPack::str_t frame) {
  if ((int)frame.length() != N) {
    return false;
  }
  for (int i = 0; i < N; i++) {
    char c = frame[i];
    pixels[i] = (c >= '0' && c <= '7') ? (uint8_t)(c - '0') : 0;
  }
  matrix.draw(pixels);
  lastFrameMs = millis();
  everHeard = true;
  return true;
}

void drawWaiting(unsigned long now) {
  memset(pixels, 0, N);
  pixels[(H - 1) * W + (now / 150) % W] = 7;
  matrix.draw(pixels);
}

void drawSilence(unsigned long now) {
  memset(pixels, 0, N);
  uint8_t level = ((now / 700) % 2) ? 7 : 2;
  for (int y = 0; y < H; y++) {
    pixels[y * W + 3 + y] = level;
    pixels[y * W + 10 - y] = level;
  }
  matrix.draw(pixels);
}

void setup() {
  matrix.begin();
  matrix.setGrayscaleBits(3);
  matrix.clear();
  Bridge.begin();
  Bridge.provide_safe("lh_frame", lhFrame);
}

void loop() {
  unsigned long now = millis();
  if (!everHeard) {
    drawWaiting(now);
  } else if (now - lastFrameMs > SILENCE_MS) {
    drawSilence(now);
  }
  delay(50);
}
