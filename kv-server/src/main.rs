#[cfg(not(target_os = "linux"))]
fn main() {
    eprintln!("monoio-kv-server requires Linux and the native io_uring driver");
    std::process::exit(2);
}

#[cfg(target_os = "linux")]
fn main() {
    if let Err(error) = run() {
        eprintln!("monoio-kv-server: {error}");
        std::process::exit(1);
    }
}

#[cfg(target_os = "linux")]
fn run() -> Result<(), Box<dyn std::error::Error>> {
    use monoio::{IoUringDriver, RuntimeBuilder};

    let config = Config::from_args(std::env::args().skip(1))?;
    let mut runtime = RuntimeBuilder::<IoUringDriver>::new().build()?;
    runtime.block_on(serve(config))?;
    Ok(())
}

#[cfg(target_os = "linux")]
#[derive(Clone)]
struct Config {
    address: std::net::SocketAddr,
    max_connections: usize,
    batch_mode: BatchMode,
    fixed_batch: usize,
}

#[cfg(target_os = "linux")]
#[derive(Clone, Copy)]
enum BatchMode {
    Fixed,
    Adaptive,
}

#[cfg(target_os = "linux")]
impl Config {
    fn from_args(args: impl Iterator<Item = String>) -> Result<Self, Box<dyn std::error::Error>> {
        let mut config = Self {
            address: "127.0.0.1:9000".parse().expect("static loopback address"),
            max_connections: 64,
            batch_mode: BatchMode::Adaptive,
            fixed_batch: 32,
        };
        let mut args = args;
        while let Some(arg) = args.next() {
            match arg.as_str() {
                "--bind" => {
                    let value = args.next().ok_or("--bind requires an address")?;
                    config.address = monoio_kv_server::parse_loopback_addr(&value)
                        .ok_or("--bind must be a literal loopback socket address")?;
                }
                "--max-connections" => {
                    config.max_connections = args
                        .next()
                        .ok_or("--max-connections requires a number")?
                        .parse()?;
                    if !(1..=64).contains(&config.max_connections) {
                        return Err("--max-connections must be in 1..=64".into());
                    }
                }
                "--batch-mode" => {
                    config.batch_mode = match args.next().as_deref() {
                        Some("fixed") => BatchMode::Fixed,
                        Some("adaptive") => BatchMode::Adaptive,
                        _ => return Err("--batch-mode must be fixed or adaptive".into()),
                    }
                }
                "--fixed-batch" => {
                    config.fixed_batch = args
                        .next()
                        .ok_or("--fixed-batch requires a number")?
                        .parse()?;
                    if !(1..=monoio_kv_server::MAX_BATCH_ITEMS).contains(&config.fixed_batch) {
                        return Err("--fixed-batch must be in 1..=32".into());
                    }
                }
                "--help" | "-h" => {
                    println!("Usage: monoio-kv-server [--bind 127.0.0.1:9000] [--max-connections 64] [--batch-mode fixed|adaptive] [--fixed-batch 32]");
                    std::process::exit(0);
                }
                _ => return Err(format!("unknown argument: {arg}").into()),
            }
        }
        Ok(config)
    }
}

#[cfg(target_os = "linux")]
#[derive(Clone)]
struct Admission {
    mode: BatchMode,
    connection_limit: usize,
    connection_ceiling: usize,
    batch_limit: usize,
    attempts: usize,
    rejected: usize,
}

#[cfg(target_os = "linux")]
impl Admission {
    fn batch_limit(&self, active: usize) -> usize {
        match self.mode {
            BatchMode::Fixed => self.batch_limit,
            BatchMode::Adaptive => {
                monoio_kv_server::adaptive_batch_limit(active, self.connection_limit)
            }
        }
    }

    fn sample(&mut self, rejected: bool, queue: usize) {
        self.attempts += 1;
        self.rejected += usize::from(rejected);
        if self.attempts == 32 {
            if matches!(self.mode, BatchMode::Adaptive) {
                self.connection_limit = monoio_kv_server::next_connection_limit(
                    self.connection_limit,
                    self.connection_ceiling,
                    self.rejected,
                );
            }
            println!(
                "ADMISSION mode={} queue_snapshot={} limit={} batch_limit={} sampled_attempts={} sampled_rejected={}",
                if matches!(self.mode, BatchMode::Adaptive) { "adaptive" } else { "fixed" },
                queue,
                self.connection_limit,
                self.batch_limit(queue),
                self.attempts,
                self.rejected
            );
            let _ = std::io::Write::flush(&mut std::io::stdout());
            self.attempts = 0;
            self.rejected = 0;
        }
    }
}

#[cfg(target_os = "linux")]
async fn serve(config: Config) -> std::io::Result<()> {
    use monoio::{net::TcpListener, spawn};
    use std::{
        cell::{Cell, RefCell},
        rc::Rc,
    };

    use monoio_kv_server::Store;

    let listener = TcpListener::bind(config.address)?;
    let active = Rc::new(Cell::new(0usize));
    let store = Rc::new(RefCell::new(Store::default()));
    let admission = Rc::new(RefCell::new(Admission {
        mode: config.batch_mode,
        connection_limit: config.max_connections,
        connection_ceiling: config.max_connections,
        batch_limit: config.fixed_batch,
        attempts: 0,
        rejected: 0,
    }));
    println!("READY driver=io_uring address={}", config.address);
    std::io::Write::flush(&mut std::io::stdout())?;

    loop {
        let (stream, peer) = listener.accept().await?;
        let count = active.get();
        let mut admission_state = admission.borrow_mut();
        let rejected = count >= admission_state.connection_limit;
        admission_state.sample(rejected, count);
        if rejected {
            drop(stream);
            eprintln!(
                "REJECT peer={peer} queue={count} limit={}",
                admission_state.connection_limit
            );
            continue;
        }
        drop(admission_state);
        active.set(count + 1);
        let active = Rc::clone(&active);
        let store = Rc::clone(&store);
        let admission = Rc::clone(&admission);
        spawn(async move {
            let _ = connection(stream, store, active.clone(), admission).await;
            active.set(active.get() - 1);
        });
    }
}

#[cfg(target_os = "linux")]
async fn connection(
    mut stream: monoio::net::TcpStream,
    store: std::rc::Rc<std::cell::RefCell<monoio_kv_server::Store>>,
    active: std::rc::Rc<std::cell::Cell<usize>>,
    admission: std::rc::Rc<std::cell::RefCell<Admission>>,
) -> std::io::Result<()> {
    use monoio::io::{AsyncReadRentExt, AsyncWriteRentExt};
    use monoio_kv_server::{BAD_REQUEST, MAX_FRAME_BYTES, OVERLOADED};
    use std::io;

    loop {
        let (read, header) = stream.read_exact(vec![0; 4]).await;
        if read.is_err() {
            return Ok(());
        }
        let len = u32::from_be_bytes(header[..].try_into().unwrap()) as usize;
        if len == 0 || len > MAX_FRAME_BYTES {
            return Err(io::Error::new(
                io::ErrorKind::InvalidData,
                "invalid frame length",
            ));
        }
        let (read, request) = stream.read_exact(vec![0; len]).await;
        if read.is_err() {
            return Ok(());
        }
        let batch_limit = {
            let admission = admission.borrow();
            admission.batch_limit(active.get())
        };
        let response = match store.borrow_mut().execute(&request, batch_limit) {
            Ok(response) => response,
            Err(monoio_kv_server::RequestError::Overloaded) => vec![OVERLOADED],
            Err(
                monoio_kv_server::RequestError::Malformed
                | monoio_kv_server::RequestError::TooLarge,
            ) => {
                vec![BAD_REQUEST]
            }
        };
        let mut frame = Vec::with_capacity(response.len() + 4);
        frame.extend_from_slice(&(response.len() as u32).to_be_bytes());
        frame.extend_from_slice(&response);
        let (written, _) = stream.write_all(frame).await;
        written?;
    }
}
