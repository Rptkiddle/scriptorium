/* Scriptorium.app launcher stub.
 *
 * The app bundle exists to give scriptorium's background processes a
 * stable macOS permission identity ("Scriptorium" in Privacy & Security,
 * scoped to the Desktop folder) instead of grants landing on generic
 * binaries like uv or zsh. The stub itself does nothing but exec the
 * dispatch script in the repo — logic lives there, editable without
 * re-signing the bundle (which would invalidate the TCC grant).
 * DISPATCH, the script's absolute path, is set by app/build.sh at compile
 * time from wherever the repository is.
 */
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

#ifndef DISPATCH
#error "build with app/build.sh, which sets DISPATCH"
#endif

int main(int argc, char *argv[]) {
    char *script = DISPATCH;
    char *args[argc + 2];
    args[0] = "/bin/zsh";
    args[1] = script;
    for (int i = 1; i < argc; i++) args[i + 1] = argv[i];
    args[argc + 1] = NULL;
    execv("/bin/zsh", args);
    perror("scriptorium stub: execv");
    return 1;
}
