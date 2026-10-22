// A bounded open-loop client; scheduled arrival time survives queueing and drops.
package main

import (
	"bytes"
	"encoding/binary"
	"encoding/json"
	"flag"
	"fmt"
	"io"
	"math"
	"math/bits"
	"math/rand"
	"net"
	"os"
	"sync"
	"time"
)

// Histogram upper edges have <=3.125% relative bucket width above 32ns.
type Histogram struct {
	Counts [2048]uint64
	Count  uint64
	MaxNS  int64
}

func (h *Histogram) add(n int64) {
	if n < 0 {
		panic("negative latency")
	}
	e := bits.Len64(uint64(n))
	index := int(n)
	if e > 6 {
		index = (e-6)*32 + int(uint64(n)>>uint(e-6))
	}
	if index >= len(h.Counts) {
		panic("latency overflow")
	}
	h.Counts[index]++
	h.Count++
	if n > h.MaxNS {
		h.MaxNS = n
	}
}
func (h *Histogram) merge(other Histogram) {
	for i, n := range other.Counts {
		h.Counts[i] += n
	}
	h.Count += other.Count
	if other.MaxNS > h.MaxNS {
		h.MaxNS = other.MaxNS
	}
}
func (h Histogram) quantile(p float64) float64 {
	if h.Count == 0 {
		return 0
	}
	target := uint64(math.Ceil(p * float64(h.Count)))
	var seen uint64
	for i, n := range h.Counts {
		seen += n
		if seen >= target {
			upper := int64(i)
			if i >= 64 {
				e := i/32 - 1
				upper = int64(((i%32)+33)<<uint(e)) - 1
			}
			if upper > h.MaxNS {
				upper = h.MaxNS
			}
			return float64(upper) / 1e6
		}
	}
	panic("histogram accounting")
}

type Stats struct {
	Offered, Started, Attempted, Completed, Rejected, Failed, TimedOut, Dropped uint64
	EndToEnd, Wire, DispatchLag                                                 Histogram
	Errors                                                                      []string
}

func (s *Stats) merge(o Stats) {
	s.Started += o.Started
	s.Attempted += o.Attempted
	s.Completed += o.Completed
	s.Rejected += o.Rejected
	s.Failed += o.Failed
	s.TimedOut += o.TimedOut
	s.EndToEnd.merge(o.EndToEnd)
	s.Wire.merge(o.Wire)
	for _, e := range o.Errors {
		if len(s.Errors) < 8 {
			s.Errors = append(s.Errors, e)
		}
	}
}
func category(index int) int {
	if index%5 == 0 {
		return 2
	}
	if index%5 < 3 {
		return 1
	}
	return 0
}

type Event struct {
	Due   time.Time
	Index int
}

func field(b []byte) []byte {
	r := make([]byte, 2, len(b)+2)
	binary.BigEndian.PutUint16(r, uint16(len(b)))
	return append(r, b...)
}
func key(i int) []byte         { return []byte(fmt.Sprintf("k%06d", i)) }
func value(i, size int) []byte { return bytes.Repeat([]byte{byte(i % 251)}, size) }
func put(i, size int) []byte {
	r := append([]byte{2}, field(key(i))...)
	r = binary.BigEndian.AppendUint32(r, uint32(size))
	return append(r, value(i, size)...)
}
func request(i, size, keys int) ([]byte, []byte) {
	k := (i * 7919) % keys
	if i%5 == 0 {
		r := []byte{3, 0, 16}
		expected := []byte{0}
		for j := 0; j < 16; j++ {
			id := (k + j) % keys
			r = append(r, field(key(id))...)
			expected = binary.BigEndian.AppendUint32(expected, uint32(size))
			expected = append(expected, value(id, size)...)
		}
		return r, expected
	}
	if i%5 < 3 {
		return put(k, size), []byte{0}
	}
	r := append([]byte{1}, field(key(k))...)
	expected := binary.BigEndian.AppendUint32([]byte{0}, uint32(size))
	return r, append(expected, value(k, size)...)
}
func rpc(conn net.Conn, payload []byte) ([]byte, error) {
	if err := conn.SetDeadline(time.Now().Add(2 * time.Second)); err != nil {
		return nil, err
	}
	frame := binary.BigEndian.AppendUint32(nil, uint32(len(payload)))
	frame = append(frame, payload...)
	for len(frame) > 0 {
		n, err := conn.Write(frame)
		if err != nil {
			return nil, err
		}
		frame = frame[n:]
	}
	header := make([]byte, 4)
	if _, err := io.ReadFull(conn, header); err != nil {
		return nil, err
	}
	size := binary.BigEndian.Uint32(header)
	if size < 1 || size > 1<<20 {
		return nil, fmt.Errorf("invalid frame %d", size)
	}
	response := make([]byte, size)
	_, err := io.ReadFull(conn, response)
	return response, err
}
func selfTest() {
	var h Histogram
	values := []int64{0, 1, 31, 32, 63, 64, 65, 1000, 1000000, 2000000000}
	for _, n := range values {
		var one Histogram
		one.add(n)
		q := one.quantile(1) * 1e6
		if q < float64(n) || q > float64(n)*1.032+1 {
			panic("bucket bounds")
		}
		h.merge(one)
	}
	if h.Count != uint64(len(values)) || h.quantile(1) != 2000 {
		panic("histogram count")
	}
	var edges Histogram
	for _, n := range []int64{64, 128, 10000} {
		edges.add(n)
	}
	if edges.quantile(.3)*1e6 != 65 || edges.quantile(.6)*1e6 != 131 {
		panic("mixed bucket edges")
	}
	for i := 0; i < 10; i++ {
		r, e := request(i, 64, 32)
		if len(r) == 0 || e[0] != 0 {
			panic("protocol")
		}
	}
	if !bytes.Equal(put(0, 2), []byte{2, 0, 7, 'k', '0', '0', '0', '0', '0', '0', 0, 0, 0, 2, 0, 0}) {
		panic("PUT encoding")
	}
	a, b := net.Pipe()
	go func() {
		defer b.Close()
		header := make([]byte, 4)
		if _, e := io.ReadFull(b, header); e != nil {
			panic(e)
		}
		body := make([]byte, binary.BigEndian.Uint32(header))
		if _, e := io.ReadFull(b, body); e != nil || !bytes.Equal(body, []byte{9}) {
			panic("framing")
		}
		for _, v := range []byte{0, 0, 0, 1, 2} {
			if _, e := b.Write([]byte{v}); e != nil {
				panic(e)
			}
		}
	}()
	r, e := rpc(a, []byte{9})
	a.Close()
	if e != nil || !bytes.Equal(r, []byte{2}) {
		panic("fragmented RPC")
	}
	fmt.Println("PASS histogram bounds, protocol encoding")
}
func main() {
	address := flag.String("address", "127.0.0.1:9000", "literal loopback service")
	clients := flag.Int("clients", 8, "persistent clients")
	size := flag.Int("bytes", 64, "value bytes")
	rate := flag.Float64("rate", 1000, "Poisson offers per second")
	seconds := flag.Float64("seconds", 10, "offering duration")
	seed := flag.Int64("seed", 20261022, "arrival seed")
	test := flag.Bool("self-test", false, "test accounting without a service")
	flag.Parse()
	if *test {
		selfTest()
		return
	}
	host, _, err := net.SplitHostPort(*address)
	if err != nil || net.ParseIP(host) == nil || !net.ParseIP(host).IsLoopback() {
		panic("numeric loopback address required")
	}
	if *clients < 1 || *clients > 64 || *size < 1 || *size > 16384 || *rate <= 0 || *rate > 100000 || *seconds <= 0 || *seconds > 300 || *rate**seconds > 2000000 {
		panic("bounded workload exceeded")
	}
	keys := 4096
	if *size > 1024 {
		keys = 512
	}
	conn, err := net.DialTimeout("tcp", *address, time.Second)
	if err != nil {
		panic(err)
	}
	for i := 0; i < keys; i++ {
		r, e := rpc(conn, put(i, *size))
		if e != nil || !bytes.Equal(r, []byte{0}) {
			panic(fmt.Sprintf("seed %d: %v %v", i, r, e))
		}
	}
	conn.Close()
	sockets := make([]net.Conn, *clients)
	for i := range sockets {
		sockets[i], err = net.DialTimeout("tcp", *address, time.Second)
		if err != nil {
			panic(err)
		}
		defer sockets[i].Close()
		r, e := rpc(sockets[i], append([]byte{1}, field(key(0))...))
		expected := append(binary.BigEndian.AppendUint32([]byte{0}, uint32(*size)), value(0, *size)...)
		if e != nil || !bytes.Equal(r, expected) {
			panic("warmup failed")
		}
	}
	arrivals := rand.New(rand.NewSource(*seed))
	offsets := []time.Duration{0}
	duration := time.Duration(*seconds * 1e9)
	for {
		next := offsets[len(offsets)-1] + time.Duration(math.Max(1, arrivals.ExpFloat64() / *rate * 1e9))
		if next >= duration {
			break
		}
		offsets = append(offsets, next)
		if len(offsets) > 2100000 {
			panic("schedule cap")
		}
	}
	queue := make(chan Event, *clients*2)
	all := make([][3]Stats, *clients)
	var wg sync.WaitGroup
	for worker := range sockets {
		wg.Add(1)
		go func(id int) {
			defer wg.Done()
			broken := false
			for event := range queue {
				s := &all[id][category(event.Index)]
				s.Started++
				if broken {
					s.Failed++
					continue
				}
				payload, expected := request(event.Index, *size, keys)
				wire := time.Now()
				s.Attempted++
				r, e := rpc(sockets[id], payload)
				ended := time.Now()
				if e != nil {
					if n, ok := e.(net.Error); ok && n.Timeout() {
						s.TimedOut++
					} else {
						s.Failed++
					}
					if len(s.Errors) < 8 {
						s.Errors = append(s.Errors, e.Error())
					}
					broken = true
					continue
				}
				if bytes.Equal(r, []byte{2}) {
					s.Rejected++
					continue
				}
				if !bytes.Equal(r, expected) {
					s.Failed++
					if len(s.Errors) < 8 {
						s.Errors = append(s.Errors, fmt.Sprintf("oracle mismatch index=%d status=%d bytes=%d", event.Index, r[0], len(r)))
					}
					continue
				}
				s.Completed++
				s.EndToEnd.add(time.Since(event.Due).Nanoseconds())
				s.Wire.add(ended.Sub(wire).Nanoseconds())
			}
		}(worker)
	}
	var result Stats
	var operations [3]Stats
	started := time.Now().Add(20 * time.Millisecond)
	for i, offset := range offsets {
		due := started.Add(offset)
		if wait := time.Until(due); wait > 0 {
			time.Sleep(wait)
		}
		result.Offered++
		operations[category(i)].Offered++
		result.DispatchLag.add(time.Since(due).Nanoseconds())
		select {
		case queue <- Event{due, i}:
		default:
			result.Dropped++
			operations[category(i)].Dropped++
		}
	}
	close(queue)
	wg.Wait()
	elapsed := time.Since(started).Seconds()
	for _, worker := range all {
		for i, s := range worker {
			result.merge(s)
			operations[i].merge(s)
		}
	}
	for _, s := range operations {
		if s.Offered != s.Completed+s.Rejected+s.Failed+s.TimedOut+s.Dropped {
			panic("per-operation accounting")
		}
	}
	if result.Offered != result.Completed+result.Rejected+result.Failed+result.TimedOut+result.Dropped || result.Started != result.Offered-result.Dropped {
		panic("outcome accounting")
	}
	percentiles := map[string]any{}
	for name, h := range map[string]Histogram{"end_to_end": result.EndToEnd, "wire": result.Wire, "dispatch_lag": result.DispatchLag} {
		percentiles[name] = map[string]any{"p50_ms_upper": h.quantile(.5), "p95_ms_upper": h.quantile(.95), "p99_ms_upper": h.quantile(.99), "count": h.Count, "max_ms": float64(h.MaxNS) / 1e6}
	}
	output := map[string]any{"stats": result, "operations": map[string]Stats{"get": operations[0], "put": operations[1], "mget16": operations[2]}, "latency": percentiles, "offering_start_unix_ns": started.UnixNano(), "offering_end_unix_ns": started.Add(duration).UnixNano(), "seconds_including_drain": elapsed, "completed_per_second": float64(result.Completed) / math.Max(elapsed, *seconds), "seed": *seed, "clients": *clients, "value_bytes": *size, "keys": keys, "rate": *rate, "offering_seconds": *seconds, "histogram": "raw 2048-bin nanosecond counts; quantiles upper edges <=3.125% bucket width; success latencies exclude rejected/failed; dispatch lag includes every offer; compare aggregate policy latency only when neither loses/rejects work"}
	if err := json.NewEncoder(os.Stdout).Encode(output); err != nil {
		panic(err)
	}
}
