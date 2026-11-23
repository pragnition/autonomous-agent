use std::path::PathBuf;

const USAGE: &str = "usage: agent-holder serve --conversation <id> [--root <dir>]";

fn flag(args: &[String], name: &str) -> Option<String> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).cloned()
}

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let root = flag(&args, "--root").map(PathBuf::from).unwrap_or_else(|| PathBuf::from(holder_proto::RUN_ROOT));
    let result = match args.first().map(String::as_str) {
        Some("serve") => {
            let Some(conversation) = flag(&args, "--conversation") else { exit_usage() };
            tokio::runtime::Runtime::new()
                .expect("tokio runtime")
                .block_on(agent_holder::serve::serve(root, conversation))
        }
        _ => exit_usage(),
    };
    if let Err(e) = result {
        eprintln!("agent-holder: {e:#}");
        std::process::exit(1);
    }
}

fn exit_usage() -> ! {
    eprintln!("{USAGE}");
    std::process::exit(2);
}
