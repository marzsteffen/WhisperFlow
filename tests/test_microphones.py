import json
import subprocess

from local_dictation.microphones import list_microphones


def test_pipewire_sources_use_stable_node_name_and_mute_state() -> None:
    payload = [
        {
            "id": 51,
            "info": {
                "props": {
                    "media.class": "Audio/Source",
                    "node.name": "alsa_input.stable",
                    "node.description": "Digitales Mikrofon",
                    "object.serial": 7183,
                }
            },
        },
        {
            "id": 65,
            "info": {"props": {"media.class": "Audio/Sink", "node.name": "ignore"}},
        },
    ]

    def runner(command, **_kwargs):
        if command[0] == "pw-dump":
            return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")
        return subprocess.CompletedProcess(command, 0, "Volume: 1.0 [MUTED]\n", "")

    microphones = list_microphones(runner=runner)
    assert len(microphones) == 1
    assert microphones[0].node_name == "alsa_input.stable"
    assert microphones[0].muted is True

