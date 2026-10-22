use std::collections::HashMap;
use std::net::SocketAddr;

pub const MAX_FRAME_BYTES: usize = 1 << 20;
pub const MAX_KEY_BYTES: usize = 1024;
pub const MAX_VALUE_BYTES: usize = 16 << 10;
pub const MAX_BATCH_ITEMS: usize = 32;
pub const MAX_STORE_BYTES: usize = 64 << 20;
pub const MAX_STORE_ENTRIES: usize = 100_000;
pub const OK: u8 = 0;
pub const BAD_REQUEST: u8 = 1;
pub const OVERLOADED: u8 = 2;

/// Parse a literal socket address only when it is bound to a loopback interface.
pub fn parse_loopback_addr(value: &str) -> Option<SocketAddr> {
    value
        .parse::<SocketAddr>()
        .ok()
        .filter(|addr| addr.ip().is_loopback())
}

pub fn adaptive_batch_limit(queue: usize, connection_limit: usize) -> usize {
    match queue.saturating_mul(4) / connection_limit.max(1) {
        0 => MAX_BATCH_ITEMS,
        1 => 24,
        2 => 16,
        _ => 8,
    }
}

pub fn next_connection_limit(limit: usize, ceiling: usize, rejected: usize) -> usize {
    if rejected > 8 {
        limit.saturating_sub((limit / 8).max(1)).max(1)
    } else if rejected <= 1 {
        (limit + (limit / 16).max(1)).min(ceiling)
    } else {
        limit.min(ceiling)
    }
}

#[derive(Default)]
pub struct Store {
    values: HashMap<Vec<u8>, Vec<u8>>,
    bytes: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum RequestError {
    Malformed,
    TooLarge,
    Overloaded,
}

impl Store {
    pub fn execute(&mut self, request: &[u8], batch_limit: usize) -> Result<Vec<u8>, RequestError> {
        let mut input = Cursor::new(request);
        let op = input.u8()?;
        let response = match op {
            1 => {
                let key = input.key()?;
                input.finish()?;
                let mut out = vec![OK];
                put_value(&mut out, self.values.get(&key).map(Vec::as_slice));
                out
            }
            2 => {
                let key = input.key()?;
                let value = input.value()?;
                input.finish()?;
                let old_cost = self.values.get(&key).map_or(0, |old| key.len() + old.len());
                let new_bytes = self.bytes - old_cost + key.len() + value.len();
                if new_bytes > MAX_STORE_BYTES
                    || (!self.values.contains_key(&key) && self.values.len() == MAX_STORE_ENTRIES)
                {
                    return Err(RequestError::Overloaded);
                }
                self.values.insert(key, value);
                self.bytes = new_bytes;
                vec![OK]
            }
            3 => {
                let count = input.count()?;
                check_count(count)?;
                let keys = (0..count)
                    .map(|_| input.key())
                    .collect::<Result<Vec<_>, _>>()?;
                input.finish()?;
                check_admission(count, batch_limit)?;
                let mut out = vec![OK];
                for key in keys {
                    put_value(&mut out, self.values.get(&key).map(Vec::as_slice));
                }
                out
            }
            4 => {
                let count = input.count()?;
                check_count(count)?;
                let entries = (0..count)
                    .map(|_| Ok((input.key()?, input.value()?)))
                    .collect::<Result<Vec<_>, RequestError>>()?;
                input.finish()?;
                check_admission(count, batch_limit)?;
                let mut staged = HashMap::with_capacity(entries.len());
                for (key, value) in entries {
                    staged.insert(key, value);
                }
                let removed = staged
                    .keys()
                    .filter_map(|key| self.values.get(key).map(|value| key.len() + value.len()))
                    .sum::<usize>();
                let added = staged
                    .iter()
                    .map(|(key, value)| key.len() + value.len())
                    .sum::<usize>();
                let new_bytes = self.bytes - removed + added;
                let new_entries = self.values.len()
                    + staged
                        .keys()
                        .filter(|key| !self.values.contains_key(*key))
                        .count();
                if new_bytes > MAX_STORE_BYTES || new_entries > MAX_STORE_ENTRIES {
                    return Err(RequestError::Overloaded);
                }
                for (key, value) in staged {
                    self.values.insert(key, value);
                }
                self.bytes = new_bytes;
                vec![OK]
            }
            _ => return Err(RequestError::Malformed),
        };
        Ok(response)
    }
}

fn check_count(count: usize) -> Result<(), RequestError> {
    if count == 0 || count > MAX_BATCH_ITEMS {
        return Err(RequestError::Malformed);
    }
    Ok(())
}

fn check_admission(count: usize, batch_limit: usize) -> Result<(), RequestError> {
    if count > batch_limit {
        return Err(RequestError::Overloaded);
    }
    Ok(())
}

fn put_value(out: &mut Vec<u8>, value: Option<&[u8]>) {
    match value {
        Some(value) => {
            out.extend_from_slice(&(value.len() as u32).to_be_bytes());
            out.extend_from_slice(value);
        }
        None => out.extend_from_slice(&u32::MAX.to_be_bytes()),
    }
}

struct Cursor<'a> {
    bytes: &'a [u8],
    at: usize,
}

impl<'a> Cursor<'a> {
    fn new(bytes: &'a [u8]) -> Self {
        Self { bytes, at: 0 }
    }

    fn take(&mut self, len: usize) -> Result<&'a [u8], RequestError> {
        let end = self.at.checked_add(len).ok_or(RequestError::Malformed)?;
        let bytes = self
            .bytes
            .get(self.at..end)
            .ok_or(RequestError::Malformed)?;
        self.at = end;
        Ok(bytes)
    }

    fn u8(&mut self) -> Result<u8, RequestError> {
        Ok(self.take(1)?[0])
    }

    fn u16(&mut self) -> Result<usize, RequestError> {
        Ok(u16::from_be_bytes(self.take(2)?.try_into().unwrap()) as usize)
    }

    fn key(&mut self) -> Result<Vec<u8>, RequestError> {
        let len = self.u16()?;
        if len == 0 || len > MAX_KEY_BYTES {
            return Err(RequestError::TooLarge);
        }
        Ok(self.take(len)?.to_vec())
    }

    fn value(&mut self) -> Result<Vec<u8>, RequestError> {
        let len = u32::from_be_bytes(self.take(4)?.try_into().unwrap()) as usize;
        if len > MAX_VALUE_BYTES {
            return Err(RequestError::TooLarge);
        }
        Ok(self.take(len)?.to_vec())
    }

    fn count(&mut self) -> Result<usize, RequestError> {
        self.u16()
    }

    fn finish(&self) -> Result<(), RequestError> {
        if self.at == self.bytes.len() {
            Ok(())
        } else {
            Err(RequestError::Malformed)
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn bind_address_must_be_loopback_literal() {
        assert_eq!(parse_loopback_addr("127.0.0.1:9000").unwrap().port(), 9000);
        assert_eq!(parse_loopback_addr("[::1]:9000").unwrap().port(), 9000);
        assert!(parse_loopback_addr("0.0.0.0:9000").is_none());
        assert!(parse_loopback_addr("192.0.2.1:9000").is_none());
        assert!(parse_loopback_addr("localhost:9000").is_none());
    }

    fn key(key: &[u8], out: &mut Vec<u8>) {
        out.extend_from_slice(&(key.len() as u16).to_be_bytes());
        out.extend_from_slice(key);
    }

    #[test]
    fn adaptive_controller_responds_to_queue_and_stays_bounded() {
        assert_eq!(adaptive_batch_limit(0, 64), 32);
        assert_eq!(adaptive_batch_limit(16, 64), 24);
        assert_eq!(adaptive_batch_limit(32, 64), 16);
        assert_eq!(adaptive_batch_limit(48, 64), 8);
        assert_eq!(next_connection_limit(64, 64, 9), 56);
        assert_eq!(next_connection_limit(56, 64, 0), 59);
        assert_eq!(next_connection_limit(1, 64, 32), 1);
        assert_eq!(next_connection_limit(64, 64, 4), 64);
        assert_eq!(next_connection_limit(64, 64, 0), 64);
    }

    fn value(value: &[u8], out: &mut Vec<u8>) {
        out.extend_from_slice(&(value.len() as u32).to_be_bytes());
        out.extend_from_slice(value);
    }

    #[test]
    fn mput_is_atomic_and_mget_reads_a_single_state() {
        let mut store = Store::default();
        let mut seed = vec![2];
        key(b"a", &mut seed);
        value(b"old", &mut seed);
        store.execute(&seed, MAX_BATCH_ITEMS).unwrap();
        let mut put = vec![4, 0, 2];
        key(b"a", &mut put);
        value(b"1", &mut put);
        key(b"bad", &mut put);
        value(&vec![0; MAX_VALUE_BYTES + 1], &mut put);
        assert_eq!(
            store.execute(&put, MAX_BATCH_ITEMS),
            Err(RequestError::TooLarge)
        );

        let mut get = vec![3, 0, 2];
        key(b"a", &mut get);
        key(b"bad", &mut get);
        let snapshot = store.execute(&get, MAX_BATCH_ITEMS).unwrap();
        assert_eq!(
            snapshot,
            [OK, 0, 0, 0, 3, b'o', b'l', b'd', 0xff, 0xff, 0xff, 0xff]
        );
    }

    #[test]
    fn mput_and_mget_round_trip_with_batch_limit() {
        let mut store = Store::default();
        let mut put = vec![4, 0, 2];
        key(b"a", &mut put);
        value(b"1", &mut put);
        key(b"b", &mut put);
        value(b"2", &mut put);
        assert_eq!(store.execute(&put, 2).unwrap(), [OK]);
        assert_eq!(store.execute(&put, 1), Err(RequestError::Overloaded));

        let mut get = vec![3, 0, 2];
        key(b"a", &mut get);
        key(b"b", &mut get);
        assert_eq!(
            store.execute(&get, 2).unwrap(),
            [OK, 0, 0, 0, 1, b'1', 0, 0, 0, 1, b'2']
        );
    }

    #[test]
    fn malformed_trailing_data_does_not_mutate_store() {
        let mut store = Store::default();
        let mut put = vec![2];
        key(b"a", &mut put);
        value(b"1", &mut put);
        put.push(0);
        assert_eq!(store.execute(&put, 1), Err(RequestError::Malformed));
        assert_eq!(
            store.execute(&[1, 0, 1, b'a'], 1).unwrap(),
            [OK, 0xff, 0xff, 0xff, 0xff]
        );
    }

    #[test]
    fn store_limit_rejects_single_and_atomic_batch_writes() {
        let mut store = Store::default();
        store
            .values
            .insert(b"z".to_vec(), vec![0; MAX_STORE_BYTES - 16_000]);
        store.bytes = MAX_STORE_BYTES - 15_999;
        store.values.insert(b"a".to_vec(), b"o".to_vec());
        store.bytes += 2;

        let mut put = vec![2];
        key(b"b", &mut put);
        value(&vec![0; MAX_VALUE_BYTES], &mut put);
        assert_eq!(
            store.execute(&put, MAX_BATCH_ITEMS),
            Err(RequestError::Overloaded)
        );

        let mut put = vec![4, 0, 2];
        key(b"a", &mut put);
        value(b"new", &mut put);
        key(b"b", &mut put);
        value(&vec![0; MAX_VALUE_BYTES], &mut put);
        assert_eq!(
            store.execute(&put, MAX_BATCH_ITEMS),
            Err(RequestError::Overloaded)
        );

        let mut get = vec![3, 0, 2];
        key(b"a", &mut get);
        key(b"b", &mut get);
        let response = store.execute(&get, MAX_BATCH_ITEMS).unwrap();
        assert_eq!(&response[..6], &[OK, 0, 0, 0, 1, b'o']);
        assert_eq!(&response[6..], &[0xff; 4]);
    }

    #[test]
    fn adaptive_overload_does_not_skip_payload_validation() {
        let mut store = Store::default();
        let mut malformed = vec![4, 0, 2];
        key(b"a", &mut malformed);
        value(b"one", &mut malformed);
        key(b"b", &mut malformed);
        assert_eq!(store.execute(&malformed, 1), Err(RequestError::Malformed));
        assert_eq!(
            store.execute(&[1, 0, 1, b'a'], 1).unwrap(),
            [OK, 0xff, 0xff, 0xff, 0xff]
        );
    }

    #[test]
    fn store_entry_count_is_bounded() {
        let mut store = Store::default();
        for index in 0..MAX_STORE_ENTRIES {
            let key = (index as u64).to_be_bytes().to_vec();
            store.bytes += key.len();
            store.values.insert(key, Vec::new());
        }
        let mut put = vec![2];
        key(&[0xff; 8], &mut put);
        value(b"", &mut put);
        assert_eq!(
            store.execute(&put, MAX_BATCH_ITEMS),
            Err(RequestError::Overloaded)
        );
        assert_eq!(store.values.len(), MAX_STORE_ENTRIES);
    }
}
