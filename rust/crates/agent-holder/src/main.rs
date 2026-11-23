use std::path::PathBuf;

const USAGE: &str = "usage: agent-holder serve|mcp --conversation <id> [--root <dir>] | agent-holder hook idle-wait";

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
        Some("mcp") => {
            let Some(conversation) = flag(&args, "--conversation") else { exit_usage() };
            let Ok(name) = holder_proto::component(&conversation) else { exit_usage() };
            let socket = root.join(name).join("holder.sock");
            tokio::runtime::Runtime::new().expect("tokio runtime").block_on(agent_holder::mcp::run(socket))
        }
        Some("hook") if args.get(1).map(String::as_str) == Some("idle-wait") => {
            let mut text = String::new();
            std::io::Read::read_to_string(&mut std::io::stdin(), &mut text).ok();
            let event = serde_json::from_str(&text).unwrap_or(serde_json::Value::Null);
            if let Some(decision) = agent_holder::idle_wait::decide(&event) {
                println!("{decision}");
            }
            Ok(())
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
