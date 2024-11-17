A Python script to play [funscripts](https://funscript.io) with MPV and [Buttplug.io](https://buttplug.io/).

See original post at on [Milovana](https://milovana.com/forum/viewtopic.php?p=363413).

# Getting started

1. Setup virtualenv for the script

We use [`pipenv`](https://pipenv.pypa.io/en/latest/) for convenience:

```sh
pipenv install
```

2. Launch the script with the video file you want to play passed as an argument

```
    pipenv run python3 ./main.py /path/to/video.mp4
```

Make sure the corresponding funscript is next to the video - i.e. in the example above, `/path/to/video.funscript`.

# Usage

- Power up your toy(s) (long-press on their respective buttons, to set them to listening mode).
- Launch Intiface central. Push big triangle button to start the engine. Go to devices, click on scan for toys.
- Once they are found, stop the scanning. Toy(s) are now identified and listed in Intiface.
- Launch the script (steps above).

## Contributors

Created by [`monsieur_so`](https://milovana.com/forum/memberlist.php?mode=viewprofile&u=104288)@milovana.

Patched were contributed by [`give_a_nap`](https://milovana.com/forum/memberlist.php?mode=viewprofile&u=122029)@milovana.