//! agentd: the control plane that replaces core and the runner.

pub mod admission;
pub mod config;
pub mod containers;
pub mod db;
pub mod handoff;
pub mod persona;
pub mod prepare;
pub mod proxy;
pub mod records;
pub mod scrub;
pub mod tasks;
pub mod turn;
pub mod usage;
