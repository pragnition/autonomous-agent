//! agentd: the control plane that replaces core and the runner.

pub mod admission;
pub mod config;
pub mod containers;
pub mod conversations;
pub mod db;
pub mod handoff;
pub mod notifier;
pub mod payloads;
pub mod persona;
pub mod prepare;
pub mod projects;
pub mod proxy;
pub mod queue;
pub mod records;
pub mod scrub;
pub mod shared;
pub mod slack;
pub mod spawned;
pub mod tasks;
pub mod turn;
pub mod usage;
