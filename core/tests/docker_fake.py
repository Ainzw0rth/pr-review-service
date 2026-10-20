"""Stateful Docker boundary double: native tests still execute real capacity locks."""

import json


class DockerFake:
    def __init__(self, *, exit_code=0, volumes=None):
        self.commands = []
        self.objects = {}
        self.exit_code = exit_code
        self.volumes = volumes
        self.counter = 0
        self.fail_remove = False

    def __call__(self, args, **kwargs):
        from core.runner import RunnerError

        self.commands.append(args)
        if args[0] == "ps":
            selected = list(self.objects)
            for flag in args:
                if flag.startswith("--filter=name=^/"):
                    name = flag.removeprefix("--filter=name=^/").removesuffix("$")
                    selected = [key for key in selected if self.objects[key]["Name"] == "/" + name]
                elif flag.startswith("--filter=id="):
                    selected = [
                        identifier
                        for identifier in selected
                        if identifier == flag.removeprefix("--filter=id=")
                    ]
                elif flag.startswith("--filter=label="):
                    key, value = flag.removeprefix("--filter=label=").split("=", 1)
                    selected = [
                        identifier
                        for identifier in selected
                        if self.objects[identifier]["Config"]["Labels"].get(key) == value
                    ]
            return "\n".join(selected)
        if args[:2] == ["volume", "inspect"]:
            return "[]"
        if args[:2] == ["image", "inspect"]:
            return json.dumps([{"Config": {"Volumes": self.volumes}}])
        if args[0] == "create":
            name = args[args.index("--name") + 1]
            if any(item["Name"] == "/" + name for item in self.objects.values()):
                raise RunnerError()
            self.counter += 1
            identifier = f"{self.counter:064x}"
            mounts = []
            for i, value in enumerate(args):
                if value == "--mount" and "target=/auth" in args[i + 1]:
                    parts = dict(part.split("=", 1) for part in args[i + 1].split(","))
                    mounts.append(
                        {"Type": "volume", "Name": parts["source"], "Destination": "/auth"}
                    )
            self.objects[identifier] = {
                "Id": identifier,
                "Name": "/" + name,
                "Config": {
                    "Labels": dict(
                        value.removeprefix("--label=").split("=", 1)
                        for value in args
                        if value.startswith("--label=")
                    )
                },
                "Mounts": mounts,
            }
            return identifier
        if args[0] == "inspect":
            if len(args) == 2:
                return json.dumps([self.objects[args[1]]])
            return json.dumps(
                {"Running": False, "ExitCode": self.exit_code, "OOMKilled": False, "Error": ""}
            )
        if args[0] == "rm":
            if self.fail_remove:
                raise RunnerError()
            del self.objects[args[-1]]
            return ""
        raise AssertionError(args)
