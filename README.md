# gitmelody

Turns the commit history of a git repository into a MIDI or WAV file.
Each commit is a note.

## Usage

```bash
pip install -r requirements.txt

python gitmelody.py ./my-repo -o history.mid
python gitmelody.py ./my-repo -o history.wav --since 2026-01-01
python gitmelody.py ./my-repo -o history.mid --instrument-map
python gitmelody.py git@github.com:me/private-repo.git -o out.mid
```

Private repositories work through your normal git setup. For a URL,
gitmelody clones into a temporary directory using whatever credentials
git already has (credential helper or SSH keys).

## Options

| Option                | Description                                                |
|-----------------------|------------------------------------------------------------|
| -o, --output          | Output file. The extension picks the format (.mid or .wav) |
| --since DATE          | Only commits after this date                               |
| --until DATE          | Only commits before this date                              |
| --branch NAME         | Branch to read. Default: current HEAD                      |
| --max-commits N       | Keep only the most recent N commits                        |
| --tempo BPM           | Tempo in beats per minute. Default: 120                    |
| --instrument-map      | Give each author a different instrument                    |

## What maps to what

| Commit property          | Sound                                |
|--------------------------|--------------------------------------|
| Lines changed            | Note length and velocity (log scale) |
| Hour of day              | Pitch (night low, day high)          |
| Time since last commit   | Rest before the note (capped)        |
| Author                   | MIDI channel, optionally instrument  |
| Revert                   | Short descending pair of notes       |
| Merge                    | Chord instead of a single note       |

Pitches are quantized to A minor pentatonic, so even bad history
does not sound completely awful.

## How it works

gitmelody runs `git log --numstat` and parses the output. Each commit
is turned into a note by a few small mapping functions, and the notes
are written with mido (MIDI) or a small stdlib synthesizer (WAV).

## Limitations

This is a toy. It does not judge code quality, ignores uncommitted
changes, and the WAV synthesis is a few sine waves.
