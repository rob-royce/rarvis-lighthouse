"""Hold the app-server pipes after OFF so its goal may continue unmanaged."""
import os
import sys


def main():
    write_fd, read_fd = map(int, sys.argv[1:3])
    # Keep the write end open without issuing any further worker commands.
    while os.read(read_fd, 65536):
        pass
    os.close(write_fd)


if __name__ == "__main__":
    main()
