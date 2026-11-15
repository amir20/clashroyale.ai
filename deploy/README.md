# Deploying Rook

Production runs on the NAS (`krazyraminfars`) in `/volume2/docker/rook`: `compose.yaml` (a copy of
`deploy/compose.yaml`), `.env` (secrets, only on the NAS) and `data/` (the database).

```sh
scripts/deploy                    # build this commit on the NAS and restart Rook
scripts/deploy --dirty            # same, with uncommitted changes
scripts/deploy --status           # which commit is running, last log lines
scripts/deploy --rollback         # back to the image that ran before the last deploy
scripts/deploy --rollback <commit>
```

The script builds with the NAS's own Docker through the docker context `krazyraminfars`, so only
changed layers rebuild and the source is streamed straight from this checkout. Each build is tagged
`rook:<commit>`; the last five are kept for rollback. After a restart Rook loads its data for about
40 seconds before reports are fast again.

## One-time setup (per Mac)

The NAS firewall drops SSH from Tailscale, so this works on the home network:

```
# ~/.ssh/config
Host krazyraminfars
  HostName 192.168.1.168
  User amirraminfar
```

```sh
docker context create krazyraminfars --docker "host=ssh://krazyraminfars"
docker --context krazyraminfars ps   # check
```

The SSH key comes from the 1Password agent and is already in the NAS's `authorized_keys`.
