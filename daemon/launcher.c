/*
 * Tiny launcher for the VoiceMode Indicator .app bundle.
 *
 * Why this exists instead of a shell script:
 *
 * macOS attributes TCC permissions (Accessibility, Microphone) to the
 * bundle's main executable. If that executable is a shell script that
 * exec's python, the running process IS python - so the permission is
 * granted to "Python 3.11", shows up under that name, and is shared with
 * every other script that uses the same interpreter.
 *
 * A real Mach-O binary inside Contents/MacOS gives the bundle its own
 * identity: it appears as "VoiceMode Indicator" in System Settings, and
 * the grant applies to this app alone.
 *
 * It does one thing: exec the venv interpreter against the indicator
 * script, keeping this process's identity by using execv (not fork).
 */

#include <unistd.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <libgen.h>
#include <signal.h>
#include <sys/wait.h>
#include <mach-o/dyld.h>

int main(int argc, char *argv[]) {
    char exec_path[4096];
    uint32_t size = sizeof(exec_path);

    if (_NSGetExecutablePath(exec_path, &size) != 0) {
        fprintf(stderr, "voicemode-indicator: executable path too long\n");
        return 1;
    }

    /* .../VoiceMode Indicator.app/Contents/MacOS/<binary>
     * We resolve the interpreter and script by absolute path rather than
     * relative to the bundle, so the bundle stays movable while the venv
     * lives with the rest of the VoiceMode data.
     */
    const char *home = getenv("HOME");
    if (!home) {
        fprintf(stderr, "voicemode-indicator: HOME unset\n");
        return 1;
    }

    char python[4096], script[4096];
    snprintf(python, sizeof(python),
             "%s/.voicemode/indicator/.venv/bin/python", home);
    snprintf(script, sizeof(script),
             "%s/.voicemode/indicator/voicemode_indicator.py", home);

    if (access(python, X_OK) != 0) {
        fprintf(stderr, "voicemode-indicator: interpreter missing at %s\n", python);
        return 1;
    }
    if (access(script, R_OK) != 0) {
        fprintf(stderr, "voicemode-indicator: script missing at %s\n", script);
        return 1;
    }

    /* Unbuffered stdout/stderr so launchd's log files stay useful. */
    setenv("PYTHONUNBUFFERED", "1", 1);

    /* FORK, don't exec-in-place.
     *
     * execv would replace THIS image with python's, so the surviving
     * process would be the interpreter again - which is exactly the
     * problem this launcher exists to solve (the grant lands on
     * "Python 3.11" instead of this app).
     *
     * By forking, the parent stays alive as the bundle's own executable.
     * macOS attributes TCC to the responsible parent process, so the
     * Accessibility grant belongs to "VoiceMode Indicator".
     */
    pid_t pid = fork();
    if (pid < 0) {
        perror("voicemode-indicator: fork failed");
        return 1;
    }

    if (pid == 0) {
        char *args[] = { python, script, NULL };
        execv(python, args);
        perror("voicemode-indicator: execv failed");
        _exit(1);
    }

    /* Forward termination to the child so launchd stop / Quit is clean
     * and we never leave an orphaned interpreter behind. */
    static pid_t child;
    child = pid;
    signal(SIGTERM, SIG_DFL);
    signal(SIGINT, SIG_DFL);

    int status = 0;
    while (waitpid(pid, &status, 0) < 0) {
        /* interrupted by a signal - keep waiting */
    }

    if (WIFEXITED(status)) return WEXITSTATUS(status);
    return 1;
}
