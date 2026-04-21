# Bash completion for coord
#
# To enable: sudo install -m 0644 scripts/completions/coord.bash /etc/bash_completion.d/coord
# Or for per-user install:
#   mkdir -p ~/.local/share/bash-completion/completions
#   cp scripts/completions/coord.bash ~/.local/share/bash-completion/completions/coord
#
# Covers top-level subcommands and their flags. No dynamic introspection.

_coord_complete() {
    local cur prev words cword
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local subcommands="start init doctor stop status claims release"
    local global_flags="--version --help -h"

    # Find the subcommand (first non-flag word after "coord").
    local sub=""
    local i
    for ((i=1; i < COMP_CWORD; i++)); do
        local w="${COMP_WORDS[i]}"
        case "$w" in
            -*) ;;
            *) sub="$w"; break ;;
        esac
    done

    if [[ -z "$sub" ]]; then
        if [[ "$cur" == -* ]]; then
            COMPREPLY=( $(compgen -W "$global_flags" -- "$cur") )
        else
            COMPREPLY=( $(compgen -W "$subcommands" -- "$cur") )
        fi
        return 0
    fi

    # Value completion for flags that take a value.
    case "$prev" in
        --tool)
            COMPREPLY=( $(compgen -W "claude codex cursor" -- "$cur") )
            return 0
            ;;
        --mode)
            COMPREPLY=( $(compgen -W "local remote" -- "$cur") )
            return 0
            ;;
        --root)
            COMPREPLY=( $(compgen -o default -- "$cur") )
            return 0
            ;;
        --host|--port|--service-url|--engineer)
            # Free-form values; fall through to default filename completion.
            COMPREPLY=( $(compgen -o default -- "$cur") )
            return 0
            ;;
    esac

    local flags=""
    case "$sub" in
        start)
            flags="--host --port --background --open-dashboard --json --help -h"
            ;;
        init)
            flags="--tool --mode --service-url --yes --no-hook --no-owners --force --root --help -h"
            ;;
        status|doctor|stop)
            flags="--help -h"
            ;;
        claims)
            flags="--engineer --all --json --help -h"
            ;;
        release)
            flags="--engineer --help -h"
            ;;
        *)
            flags="--help -h"
            ;;
    esac

    if [[ "$cur" == -* ]]; then
        COMPREPLY=( $(compgen -W "$flags" -- "$cur") )
    else
        COMPREPLY=( $(compgen -o default -- "$cur") )
    fi
    return 0
}

complete -F _coord_complete coord
