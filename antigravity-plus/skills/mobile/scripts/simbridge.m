// Antigravity Plus — /mobile: touches, text and the list of on-screen elements for an iOS simulator.
//
// `xcrun simctl` boots simulators, installs apps and takes screenshots, but it cannot tap, swipe or type, and it
// cannot say what is on the screen. This helper does those through the same frameworks Simulator.app itself uses
// (CoreSimulator, SimulatorKit, AccessibilityPlatformTranslation), so nothing has to be installed besides Xcode.
// mobile.py compiles it once (`xcrun clang`) and runs it:
//
//   simbridge DEVELOPER_DIR UDID probe
//   simbridge DEVELOPER_DIR UDID tap X Y [HOLD_MS]              X, Y: 0…1 of the screen's width and height
//   simbridge DEVELOPER_DIR UDID swipe X1 Y1 X2 Y2 [MS] [EDGE]  EDGE: 1 left, 2 right, 3 top, 4 bottom (system gestures)
//   simbridge DEVELOPER_DIR UDID ui                             the accessibility tree of the frontmost app, as JSON
//   simbridge DEVELOPER_DIR UDID settext X Y TEXT               X, Y: a point (in points) inside a text field
//
// Text goes in through accessibility, the way VoiceOver's dictation does: it works with any language and keyboard
// layout. Hardware keys and buttons are not sent: on Xcode 27 the simulator ignores them from an outside process.
// The frameworks are private, so every answer from them is checked before it is used: a touch message whose layout
// isn't the one this file knows is never sent (exit code 3 — mobile.py then says what still works without touches).
// Exit codes: 0 done, 2 usage or no such device, 3 this Xcode isn't supported, 4 the simulator didn't answer.

#import <Foundation/Foundation.h>
#import <CoreGraphics/CoreGraphics.h>
#import <objc/runtime.h>
#import <objc/message.h>
#import <dlfcn.h>
#import <malloc/malloc.h>

typedef unsigned char *(*MouseFn)(CGPoint *first, CGPoint *second, unsigned int target, unsigned long type,
                                  unsigned long edge, double width, double height);

enum { kDown = 1, kUp = 2, kMove = 6 };    // NSEventType: left mouse down, up, dragged
enum { kTouchTarget = 0x32 };              // the digitizer
enum { kPayload = 0xa0, kHeader = 0x20 };  // the message layout this file knows (Xcode 27)
static const double kFrame = 0.017;       // SimulatorKit drops moves that come sooner than 16 ms apart

static id gDevice, gClient;
static dispatch_queue_t gQueue;
static MouseFn gMouse;

static void fail(int code, NSString *format, ...) NS_FORMAT_FUNCTION(2, 3);
static void fail(int code, NSString *format, ...) {
  va_list args;
  va_start(args, format);
  NSString *message = [[NSString alloc] initWithFormat:format arguments:args];
  va_end(args);
  fprintf(stderr, "simbridge: %s\n", message.UTF8String);
  exit(code);
}

static id send0(id target, const char *selector) {
  SEL sel = sel_registerName(selector);
  return [target respondsToSelector:sel] ? ((id (*)(id, SEL))objc_msgSend)(target, sel) : nil;
}

// ---------------------------------------------------------------------------------------------------- frameworks

static void loadFrameworks(NSString *developerDir) {
  if (!dlopen("/Library/Developer/PrivateFrameworks/CoreSimulator.framework/CoreSimulator", RTLD_NOW))
    fail(3, @"CoreSimulator.framework didn't load: %s", dlerror());
  NSArray<NSString *> *places = @[
    [[developerDir stringByDeletingLastPathComponent] stringByAppendingPathComponent:@"SharedFrameworks"],
    [developerDir stringByAppendingPathComponent:@"Library/PrivateFrameworks"],
  ];
  void *kit = NULL;
  for (NSString *place in places) {
    NSString *path = [place stringByAppendingPathComponent:@"SimulatorKit.framework/SimulatorKit"];
    if ((kit = dlopen(path.fileSystemRepresentation, RTLD_NOW))) break;
  }
  if (!kit) fail(3, @"SimulatorKit.framework not found under %@", developerDir);
  gMouse = (MouseFn)dlsym(kit, "IndigoHIDMessageForMouseNSEvent");
  dlopen("/System/Library/PrivateFrameworks/AccessibilityPlatformTranslation.framework/AccessibilityPlatformTranslation", RTLD_NOW);
}

static id findDevice(NSString *developerDir, NSString *udid) {
  Class contextClass = objc_getClass("SimServiceContext");
  SEL shared = sel_registerName("sharedServiceContextForDeveloperDir:error:");
  if (!contextClass || ![contextClass respondsToSelector:shared]) fail(3, @"CoreSimulator has no SimServiceContext");
  NSError *error = nil;
  id context = ((id (*)(id, SEL, id, NSError **))objc_msgSend)(contextClass, shared, developerDir, &error);
  if (!context) fail(4, @"no CoreSimulator service: %@", error.localizedDescription);
  SEL defaultSet = sel_registerName("defaultDeviceSetWithError:");
  if (![context respondsToSelector:defaultSet]) fail(3, @"CoreSimulator has no default device set");
  id set = ((id (*)(id, SEL, NSError **))objc_msgSend)(context, defaultSet, &error);
  if (!set) fail(4, @"no simulator device set: %@", error.localizedDescription);
  for (id device in (NSArray *)send0(set, "devices")) {
    NSUUID *uuid = send0(device, "UDID");
    if ([uuid isKindOfClass:NSUUID.class] && [uuid.UUIDString caseInsensitiveCompare:udid] == NSOrderedSame) return device;
  }
  fail(2, @"no simulator with UDID %@", udid);
  return nil;
}

static BOOL booted(id device) {
  SEL state = sel_registerName("state");
  return [device respondsToSelector:state] && ((unsigned long (*)(id, SEL))objc_msgSend)(device, state) == 3;
}

// --------------------------------------------------------------------------------------------------------- input

static void openClient(void) {
  Class clientClass = objc_getClass("_TtC12SimulatorKit24SimDeviceLegacyHIDClient")
      ?: objc_getClass("SimulatorKit.SimDeviceLegacyHIDClient") ?: objc_getClass("SimDeviceLegacyHIDClient");
  SEL init = sel_registerName("initWithDevice:error:");
  SEL send = sel_registerName("sendWithMessage:freeWhenDone:completionQueue:completion:");
  if (!clientClass || ![clientClass instancesRespondToSelector:init] || ![clientClass instancesRespondToSelector:send])
    fail(3, @"SimulatorKit has no HID client this helper knows");
  NSError *error = nil;
  gClient = ((id (*)(id, SEL, id, NSError **))objc_msgSend)([clientClass alloc], init, gDevice, &error);
  if (!gClient) fail(4, @"the simulator didn't accept an input connection: %@", error.localizedDescription);
}

static void deliver(unsigned char *message) {
  dispatch_semaphore_t done = dispatch_semaphore_create(0);
  __block NSError *failure = nil;
  SEL send = sel_registerName("sendWithMessage:freeWhenDone:completionQueue:completion:");
  ((void (*)(id, SEL, void *, BOOL, id, id))objc_msgSend)(gClient, send, message, YES, gQueue, ^(NSError *error) {
    failure = error;
    dispatch_semaphore_signal(done);
  });
  if (dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 5 * NSEC_PER_SEC)))
    fail(4, @"the simulator didn't take the input event in 5 s");
  if (failure) fail(4, @"the simulator refused the input event: %@", failure.localizedDescription);
}

static BOOL near(const unsigned char *bytes, double want) {
  double got;
  memcpy(&got, bytes, sizeof got);
  return fabs(got - want) < 1e-6;
}

/// A touch event at (x, y) — 0…1 of the screen. Returns NO when SimulatorKit dropped it (a move too soon after the
/// previous event). Stops the program if the message doesn't look the way this file expects.
static BOOL touch(double x, double y, unsigned long type, unsigned long edge) {
  CGPoint point = CGPointMake(x, y);
  unsigned char *message = gMouse(&point, NULL, kTouchTarget, type, edge, 1.0, 1.0);
  if (!message) {
    if (type == kMove) return NO;
    fail(3, @"SimulatorKit built no touch message");
  }
  unsigned int inner, kind;
  memcpy(&inner, message + 0x18, 4);
  memcpy(&kind, message + kHeader, 4);
  size_t first = kHeader + 0x1c, second = kHeader + kPayload + 0x1c;   // where each payload keeps x and y
  if (malloc_size(message) < kHeader + 2 * kPayload || inner != kPayload || kind != 0x0b
      || !near(message + first, x) || !near(message + first + 8, y)
      || !near(message + second, x) || !near(message + second + 8, y)) {
    free(message);
    fail(3, @"this Xcode builds touch events in a format this helper doesn't know (inner size 0x%x)", inner);
  }
  deliver(message);
  return YES;
}

static void pause_(double seconds) {
  if (seconds > 0) usleep((useconds_t)(seconds * 1e6));
}

static double clamp(double value) {
  return value < 0 ? 0 : value > 1 ? 1 : value;
}

static void tap(double x, double y, double hold) {
  touch(x, y, kDown, 0);
  pause_(hold > 0.06 ? hold : 0.06);
  touch(x, y, kUp, 0);
}

static void swipe(double x1, double y1, double x2, double y2, double seconds, unsigned long edge) {
  int steps = (int)(seconds / kFrame);
  if (steps < 6) steps = 6;
  touch(x1, y1, kDown, edge);
  pause_(kFrame);
  for (int i = 1; i <= steps; i++) {
    double t = (double)i / steps;
    t = t * t * (3 - 2 * t);  // ease in and out: a finger doesn't start or stop at full speed
    touch(x1 + (x2 - x1) * t, y1 + (y2 - y1) * t, kMove, edge);
    pause_(kFrame);
  }
  pause_(0.05);             // hold still before lifting, so a drag doesn't turn into a fling
  touch(x2, y2, kUp, edge);
}

// ------------------------------------------------------------------------------------------- what's on the screen

static NSString *gToken;
static NSDate *gDeadline;
static int gElements;
static BOOL gCut;

@interface AGPBridge : NSObject
@end

@implementation AGPBridge
// AccessibilityPlatformTranslation asks for each attribute through this block; CoreSimulator carries the request
// into the simulator and brings the answer back.
- (id)accessibilityTranslationDelegateBridgeCallbackWithToken:(NSString *)token {
  return ^id(id request) {
    dispatch_semaphore_t done = dispatch_semaphore_create(0);
    __block id response = nil;
    SEL send = sel_registerName("sendAccessibilityRequestAsync:completionQueue:completionHandler:");
    ((void (*)(id, SEL, id, id, id))objc_msgSend)(gDevice, send, request, gQueue, ^(id answer) {
      response = answer;
      dispatch_semaphore_signal(done);
    });
    dispatch_semaphore_wait(done, dispatch_time(DISPATCH_TIME_NOW, 4 * NSEC_PER_SEC));
    if (response) return response;
    Class responseClass = objc_getClass("AXPTranslatorResponse");
    SEL empty = sel_registerName("emptyResponse");
    return [responseClass respondsToSelector:empty] ? ((id (*)(id, SEL))objc_msgSend)(responseClass, empty) : nil;
  };
}
- (CGRect)accessibilityTranslationConvertPlatformFrameToSystem:(CGRect)rect withToken:(NSString *)token {
  return rect;
}
- (id)accessibilityTranslationRootParentWithToken:(NSString *)token {
  return nil;
}
@end

static void claim(id element) {
  id translation = send0(element, "translation");
  SEL setToken = sel_registerName("setBridgeDelegateToken:");
  if ([translation respondsToSelector:setToken]) ((void (*)(id, SEL, id))objc_msgSend)(translation, setToken, gToken);
}

static NSString *text(id element, const char *selector) {
  id value = send0(element, selector);
  if ([value isKindOfClass:NSString.class]) return [value length] ? value : nil;
  if ([value isKindOfClass:NSNumber.class]) return [value stringValue];
  if ([value isKindOfClass:NSAttributedString.class]) return [value string].length ? [value string] : nil;
  return nil;
}

/// Where an element is, in points; CGRectNull when it doesn't say (or says something that isn't a rectangle).
static CGRect frameOf(id element) {
  SEL frame = sel_registerName("accessibilityFrame");
  if (![element respondsToSelector:frame]) return CGRectNull;
#if defined(__x86_64__)  // a struct this size comes back through a different entry point there
  CGRect rect = ((CGRect (*)(id, SEL))objc_msgSend_stret)(element, frame);
#else
  CGRect rect = ((CGRect (*)(id, SEL))objc_msgSend)(element, frame);
#endif
  BOOL finite = isfinite(rect.origin.x) && isfinite(rect.origin.y) && isfinite(rect.size.width) && isfinite(rect.size.height);
  return finite ? rect : CGRectNull;
}

/// Print an answer for mobile.py. Stops the program when it can't be written as JSON.
static void answer(NSDictionary *object) {
  NSData *json = [NSJSONSerialization isValidJSONObject:object] ? [NSJSONSerialization dataWithJSONObject:object options:0 error:NULL] : nil;
  if (!json) fail(4, @"the simulator's answer couldn't be written out");
  fwrite(json.bytes, 1, json.length, stdout);
  fputc('\n', stdout);
}

static NSDictionary *describe(id element, int depth) {
  claim(element);
  gElements++;
  NSMutableDictionary *out = [NSMutableDictionary dictionary];
  CGRect rect = frameOf(element);
  if (!CGRectIsNull(rect)) out[@"frame"] = @[@(rect.origin.x), @(rect.origin.y), @(rect.size.width), @(rect.size.height)];
  NSDictionary<NSString *, NSString *> *fields = @{
    @"role": @"accessibilityRole", @"label": @"accessibilityLabel", @"value": @"accessibilityValue",
    @"id": @"accessibilityIdentifier", @"title": @"accessibilityTitle", @"subrole": @"accessibilitySubrole",
    @"placeholder": @"accessibilityPlaceholderValue",
  };
  for (NSString *name in fields) {
    NSString *value = text(element, fields[name].UTF8String);
    if (value) out[name] = value;
  }
  SEL enabled = sel_registerName("isAccessibilityEnabled");
  if ([element respondsToSelector:enabled] && !((BOOL (*)(id, SEL))objc_msgSend)(element, enabled)) out[@"disabled"] = @YES;
  SEL focused = sel_registerName("isAccessibilityFocused");
  if ([element respondsToSelector:focused] && ((BOOL (*)(id, SEL))objc_msgSend)(element, focused)) out[@"focused"] = @YES;
  SEL selected = sel_registerName("isAccessibilitySelected");
  if ([element respondsToSelector:selected] && ((BOOL (*)(id, SEL))objc_msgSend)(element, selected)) out[@"selected"] = @YES;
  if (depth >= 60 || gElements >= 1500 || [gDeadline timeIntervalSinceNow] < 0) {
    gCut = YES;
    return out;
  }
  NSMutableArray *children = [NSMutableArray array];
  for (id child in (NSArray *)send0(element, "accessibilityChildren")) {
    [children addObject:describe(child, depth + 1)];
    if (gCut && [gDeadline timeIntervalSinceNow] < 0) break;
  }
  if (children.count) out[@"children"] = children;
  return out;
}

static id gTranslation;

/// The frontmost app as an accessibility element. Stops the program when the bridge isn't there or doesn't answer.
static id frontmostElement(void) {
  Class translatorClass = objc_getClass("AXPTranslator");
  id translator = [translatorClass respondsToSelector:sel_registerName("sharedInstance")]
      ? ((id (*)(id, SEL))objc_msgSend)(translatorClass, sel_registerName("sharedInstance")) : nil;
  SEL frontmost = sel_registerName("frontmostApplicationWithDisplayId:bridgeDelegateToken:");
  SEL platform = sel_registerName("macPlatformElementFromTranslation:");
  SEL request = sel_registerName("sendAccessibilityRequestAsync:completionQueue:completionHandler:");
  if (!translator || ![translator respondsToSelector:frontmost] || ![translator respondsToSelector:platform]
      || ![translator respondsToSelector:sel_registerName("setBridgeTokenDelegate:")]
      || ![gDevice respondsToSelector:request])
    fail(3, @"this macOS and Xcode have no accessibility bridge this helper knows");
  static AGPBridge *bridge;  // the translator keeps only a weak reference
  bridge = [AGPBridge new];
  gToken = NSUUID.UUID.UUIDString;
  gDeadline = [NSDate dateWithTimeIntervalSinceNow:20];
  ((void (*)(id, SEL, id))objc_msgSend)(translator, sel_registerName("setBridgeTokenDelegate:"), bridge);
  if ([translator respondsToSelector:sel_registerName("setSupportsDelegateTokens:")])
    ((void (*)(id, SEL, BOOL))objc_msgSend)(translator, sel_registerName("setSupportsDelegateTokens:"), YES);
  gTranslation = ((id (*)(id, SEL, unsigned int, id))objc_msgSend)(translator, frontmost, 0, gToken);
  if (!gTranslation) fail(4, @"the simulator named no frontmost app (is it still starting?)");
  ((void (*)(id, SEL, id))objc_msgSend)(gTranslation, sel_registerName("setBridgeDelegateToken:"), gToken);
  id root = ((id (*)(id, SEL, id))objc_msgSend)(translator, platform, gTranslation);
  if (!root) fail(4, @"the simulator gave no accessibility tree");
  return root;
}

static void ui(void) {
  NSMutableDictionary *tree = [describe(frontmostElement(), 0) mutableCopy];
  SEL pid = sel_registerName("pid");
  if ([gTranslation respondsToSelector:pid]) tree[@"pid"] = @(((int (*)(id, SEL))objc_msgSend)(gTranslation, pid));
  if (gCut) tree[@"cut"] = @YES;
  answer(tree);
}

/// The deepest text field whose frame holds the point.
static id fieldAt(id element, CGPoint point, int depth) {
  claim(element);
  if (depth > 60 || [gDeadline timeIntervalSinceNow] < 0) return nil;
  for (id child in (NSArray *)send0(element, "accessibilityChildren")) {
    id found = fieldAt(child, point, depth + 1);
    if (found) return found;
  }
  NSString *role = text(element, "accessibilityRole");
  if (!role) return nil;
  BOOL field = [@[@"AXTextField", @"AXTextArea", @"AXSearchField", @"AXSecureTextField", @"AXComboBox"] containsObject:role];
  return field && CGRectContainsPoint(frameOf(element), point) ? element : nil;
}

static void setText(double x, double y, NSString *value) {
  id root = frontmostElement();
  id field = fieldAt(root, CGPointMake(x, y), 0);
  SEL set = sel_registerName("setAccessibilityValue:");
  if (!field || ![field respondsToSelector:set]) fail(2, @"no text field at (%.0f, %.0f)", x, y);
  ((void (*)(id, SEL, id))objc_msgSend)(field, set, value);
  usleep(400000);  // the app handles the change on its own run loop
  answer(@{@"value": text(field, "accessibilityValue") ?: @"", @"role": text(field, "accessibilityRole") ?: @""});
}

// ---------------------------------------------------------------------------------------------------------- main

int main(int argc, char **argv) {
  @autoreleasepool {
    if (argc < 4) fail(2, @"usage: simbridge DEVELOPER_DIR UDID probe|tap|swipe|ui|settext …");
    NSString *developerDir = @(argv[1]), *udid = @(argv[2]), *verb = @(argv[3]);
    char **rest = argv + 4;
    int count = argc - 4;
    loadFrameworks(developerDir);
    gDevice = findDevice(developerDir, udid);
    gQueue = dispatch_queue_create("antigravity-plus.simbridge", DISPATCH_QUEUE_SERIAL);
    if ([verb isEqualToString:@"probe"]) {
      printf("{\"device\": \"%s\", \"booted\": %s, \"input\": %s, \"ui\": %s}\n", [send0(gDevice, "name") UTF8String] ?: "",
             booted(gDevice) ? "true" : "false", gMouse ? "true" : "false", objc_getClass("AXPTranslator") ? "true" : "false");
      return gMouse ? 0 : 3;
    }
    if (!booted(gDevice)) fail(2, @"the simulator isn't booted");
    if ([verb isEqualToString:@"ui"]) {
      ui();
    } else if ([verb isEqualToString:@"settext"] && count >= 3) {
      setText(atof(rest[0]), atof(rest[1]), @(rest[2]));
    } else if ([verb isEqualToString:@"tap"] && count >= 2) {
      if (!gMouse) fail(3, @"SimulatorKit has no touch function this helper knows");
      openClient();
      tap(clamp(atof(rest[0])), clamp(atof(rest[1])), count > 2 ? atof(rest[2]) / 1000 : 0);
      pause_(0.05);  // let the last event leave before the connection closes
    } else if ([verb isEqualToString:@"swipe"] && count >= 4) {
      if (!gMouse) fail(3, @"SimulatorKit has no touch function this helper knows");
      openClient();
      swipe(clamp(atof(rest[0])), clamp(atof(rest[1])), clamp(atof(rest[2])), clamp(atof(rest[3])),
            count > 4 ? atof(rest[4]) / 1000 : 0.3, count > 5 ? strtoul(rest[5], NULL, 10) : 0);
      pause_(0.05);
    } else {
      fail(2, @"unknown command: %@", verb);
    }
  }
  return 0;
}
