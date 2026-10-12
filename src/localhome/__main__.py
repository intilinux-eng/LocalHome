"""`python -m localhome`, and the entry point Nuitka compiles into the
.deb's binary (packaging/build.sh): the web dashboard - or, with `cli`
first, localhome-cli's commands (`localhome cli doctor`)."""
import sys

if __name__ == "__main__":
    if sys.argv[1:2] == ["cli"]:
        del sys.argv[1]
        from localhome.cli import main
    else:
        from localhome.web.app import main
    main()
