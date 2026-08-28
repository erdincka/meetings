# Normalise BUILDER into something rsync and ssh both accept. Sourced, not run.
#
# BUILDER is an SSH destination, because `make images` rsyncs the tree to the
# builder and runs docker there. It is easy to write it as `ssh://user@host`
# instead -- that is the form `docker context create --docker host=...` wants,
# and the two settings do the same job in a reader's head. They are not
# interchangeable: ssh(1) accepts the URL form, rsync(1) does not, and parses
# `ssh://user@host:/dir` as the host `ssh` plus a path. The result is
#
#     ssh: Could not resolve hostname ssh
#
# which names neither BUILDER nor the scheme that caused it. So accept both
# spellings, and reject only what genuinely cannot work.
#
# printf rather than a heredoc throughout: this file is sourced into whatever
# shell the caller happens to be, and `cat` is a popular thing to alias.
normalize_builder() {
  local raw=${1:-}
  [ -z "$raw" ] && { printf '%s' ""; return 0; }

  local target=${raw#ssh://}

  # A port survives the scheme strip as `host:22`, and rsync reads that colon as
  # the start of the path. Carrying it properly would mean -e 'ssh -p N' here
  # and a URL there; a Host alias in ~/.ssh/config does it once, for both.
  local hostport=${target##*@} user=""
  case "$target" in *@*) user=${target%%@*} ;; esac

  case "$hostport" in
    *:*)
      printf '%s\n' \
        "BUILDER='$raw' has a port in it, which rsync cannot use." \
        "" \
        "Put the port in ~/.ssh/config and name the alias here instead:" \
        "" \
        "    Host builder" \
        "        HostName ${hostport%%:*}" \
        "        Port ${hostport##*:}" >&2
      [ -n "$user" ] && printf '        User %s\n' "$user" >&2
      printf '%s\n' "" "    BUILDER=builder" >&2
      return 1
      ;;
  esac

  printf '%s' "$target"
}
