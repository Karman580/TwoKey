// Command devledger is a single-process stand-in for the Drunix network, used
// for local demos and tests where Docker and a Fabric network are not
// available. It runs the same credit state machine as the chaincode and
// enforces endorsement cryptographically: every transaction carries Ed25519
// signatures from organisations, and a state change commits only when the
// credit's policy (both banks, plus NPCI when disputed) is satisfied.
//
// It is not a blockchain: there is no ordering service, no consensus and no
// peers. It keeps a hash-chained log of committed transactions so the history
// is tamper-evident, and it replays that log on restart.
package main

import (
	"bufio"
	"crypto/ed25519"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"log"
	"net/http"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/Karman580/TwoKey/chaincode/credit"
)

// Endorsement is one organisation's signature over a proposal.
type Endorsement struct {
	Org string `json:"org"`
	Sig string `json:"sig"` // base64 Ed25519 signature over SigningBytes
}

// Proposal is a transaction submitted to the ledger. Args is a JSON string and
// is signed byte-for-byte, so no canonical JSON is needed.
type Proposal struct {
	TxID         string        `json:"txId"`
	Fn           string        `json:"fn"`
	Creator      string        `json:"creator"`
	Args         string        `json:"args"`
	Endorsements []Endorsement `json:"endorsements"`
}

// SigningBytes is what every endorser signs.
func (p Proposal) SigningBytes() []byte {
	return []byte("twokey-v1\n" + p.TxID + "\n" + p.Fn + "\n" + p.Creator + "\n" + p.Args)
}

// Block is one committed transaction in the hash chain.
type Block struct {
	Height    int      `json:"height"`
	TxID      string   `json:"txId"`
	Fn        string   `json:"fn"`
	Creator   string   `json:"creator"`
	Endorsers []string `json:"endorsers"`
	TxnRef    string   `json:"txnRef"`
	At        int64    `json:"at"`
	PrevHash  string   `json:"prevHash"`
	Hash      string   `json:"hash"`
}

type logEntry struct {
	Proposal Proposal `json:"proposal"`
	At       int64    `json:"at"`
}

// Ledger holds world state. ponytail: one global lock; fine for a demo
// ledger, a real network orders transactions instead.
type Ledger struct {
	mu      sync.Mutex
	keys    map[string]ed25519.PublicKey
	credits map[string]*credit.ConditionalCredit
	seen    map[string]bool
	chain   []Block
	props   []Proposal // committed proposals, parallel to chain
	logFile *os.File
	offset  int64 // dev clock offset in seconds
	clock   func() int64
}

// TxError is a rejected transaction with an HTTP status and a stable code.
type TxError struct {
	Status int
	Code   string
	Msg    string
}

func (e *TxError) Error() string { return e.Code + ": " + e.Msg }

func newLedger(keys map[string]ed25519.PublicKey) *Ledger {
	return &Ledger{keys: keys, credits: map[string]*credit.ConditionalCredit{},
		seen: map[string]bool{}, clock: func() int64 { return time.Now().Unix() }}
}

func (l *Ledger) now() int64 { return l.clock() + l.offset }

type refArgs struct {
	TxnRef string `json:"txnRef"`
	Arg    string `json:"arg"`
}

// Submit verifies and commits a proposal at time at.
func (l *Ledger) Submit(p Proposal, at int64) (*credit.ConditionalCredit, *Block, error) {
	if p.TxID == "" || p.Fn == "" || p.Creator == "" {
		return nil, nil, &TxError{400, "BAD_REQUEST", "txId, fn and creator are required"}
	}
	if l.seen[p.TxID] {
		return nil, nil, &TxError{409, "REPLAY", "transaction " + p.TxID + " was already committed"}
	}
	msg := p.SigningBytes()
	var endorsers []string
	for _, e := range p.Endorsements {
		pub, ok := l.keys[e.Org]
		sig, err := base64.StdEncoding.DecodeString(e.Sig)
		if !ok || err != nil || !ed25519.Verify(pub, msg, sig) {
			return nil, nil, &TxError{401, "INVALID_SIGNATURE", "endorsement claiming to be " + e.Org + " does not verify against its public key"}
		}
		endorsers = append(endorsers, e.Org)
	}
	creatorSigned := false
	for _, e := range endorsers {
		creatorSigned = creatorSigned || e == p.Creator
	}
	if !creatorSigned {
		return nil, nil, &TxError{401, "INVALID_SIGNATURE", "creator " + p.Creator + " did not sign the proposal"}
	}
	tx := credit.Tx{ID: p.TxID, Creator: p.Creator, Endorsers: endorsers, Now: at}

	var next *credit.ConditionalCredit
	var txnRef string
	if p.Fn == "CreateHold" {
		var req credit.HoldRequest
		if err := json.Unmarshal([]byte(p.Args), &req); err != nil {
			return nil, nil, &TxError{400, "BAD_REQUEST", "args: " + err.Error()}
		}
		txnRef = req.TxnRef
		if c, ok := l.credits[req.TxnRef]; ok {
			if credit.SameHold(c, req) {
				return c, nil, nil // idempotent retry, nothing new to commit
			}
			return nil, nil, &TxError{409, "CONFLICT", "txnRef " + req.TxnRef + " already holds a different credit"}
		}
		probe := &credit.ConditionalCredit{PayerBank: req.PayerBank, PayeeBank: req.PayeeBank}
		if err := credit.CheckEndorsement(probe, endorsers); err != nil {
			return nil, nil, &TxError{403, "ENDORSEMENT_POLICY_FAILURE", err.Error()}
		}
		c, err := credit.NewHold(req, tx)
		if err != nil {
			return nil, nil, &TxError{422, "REJECTED", err.Error()}
		}
		next = c
	} else {
		var a refArgs
		if err := json.Unmarshal([]byte(p.Args), &a); err != nil {
			return nil, nil, &TxError{400, "BAD_REQUEST", "args: " + err.Error()}
		}
		txnRef = a.TxnRef
		c, ok := l.credits[a.TxnRef]
		if !ok {
			return nil, nil, &TxError{404, "NOT_FOUND", "no credit " + a.TxnRef}
		}
		if err := credit.CheckEndorsement(c, endorsers); err != nil {
			return nil, nil, &TxError{403, "ENDORSEMENT_POLICY_FAILURE", err.Error()}
		}
		n, err := credit.Apply(c, p.Fn, a.Arg, tx)
		if err != nil {
			return nil, nil, &TxError{422, "REJECTED", err.Error()}
		}
		next = n
	}

	if l.logFile != nil {
		b, _ := json.Marshal(logEntry{p, at})
		if _, err := l.logFile.Write(append(b, '\n')); err != nil {
			return nil, nil, &TxError{500, "STORAGE", err.Error()}
		}
	}
	sort.Strings(endorsers)
	prev := strings.Repeat("0", 64)
	if len(l.chain) > 0 {
		prev = l.chain[len(l.chain)-1].Hash
	}
	blk := Block{Height: len(l.chain) + 1, TxID: p.TxID, Fn: p.Fn, Creator: p.Creator,
		Endorsers: endorsers, TxnRef: txnRef, At: at, PrevHash: prev}
	blk.Hash = blockHash(blk, p)
	l.chain = append(l.chain, blk)
	l.props = append(l.props, p)
	l.seen[p.TxID] = true
	l.credits[txnRef] = next
	return next, &blk, nil
}

func blockHash(b Block, p Proposal) string {
	h := sha256.New()
	fmt.Fprintf(h, "%d\n%s\n%d\n", b.Height, b.PrevHash, b.At)
	h.Write(p.SigningBytes())
	return hex.EncodeToString(h.Sum(nil))
}

// replay rebuilds state from the transaction log.
func (l *Ledger) replay(path string) error {
	f, err := os.Open(path)
	if errors.Is(err, os.ErrNotExist) {
		return nil
	}
	if err != nil {
		return err
	}
	defer f.Close()
	sc := bufio.NewScanner(f)
	sc.Buffer(make([]byte, 1<<20), 1<<20)
	for sc.Scan() {
		var e logEntry
		if err := json.Unmarshal(sc.Bytes(), &e); err != nil {
			return fmt.Errorf("corrupt log line %d: %w", len(l.chain)+1, err)
		}
		if _, _, err := l.Submit(e.Proposal, e.At); err != nil {
			return fmt.Errorf("replaying %s: %w", e.Proposal.TxID, err)
		}
	}
	return sc.Err()
}

func (l *Ledger) verifyChain() (bool, int) {
	prev := strings.Repeat("0", 64)
	for i, b := range l.chain {
		if b.PrevHash != prev || b.Height != i+1 || blockHash(b, l.props[i]) != b.Hash {
			return false, i + 1
		}
		prev = b.Hash
	}
	return true, len(l.chain)
}

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func (l *Ledger) routes(allowClock bool) http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /health", func(w http.ResponseWriter, r *http.Request) {
		writeJSON(w, 200, map[string]any{"ok": true})
	})
	mux.HandleFunc("GET /time", func(w http.ResponseWriter, r *http.Request) {
		l.mu.Lock()
		defer l.mu.Unlock()
		writeJSON(w, 200, map[string]any{"now": l.now(), "offsetSeconds": l.offset})
	})
	mux.HandleFunc("POST /tx", func(w http.ResponseWriter, r *http.Request) {
		var p Proposal
		if err := json.NewDecoder(http.MaxBytesReader(w, r.Body, 64<<10)).Decode(&p); err != nil {
			writeJSON(w, 400, map[string]string{"code": "BAD_REQUEST", "error": err.Error()})
			return
		}
		l.mu.Lock()
		c, blk, err := l.Submit(p, l.now())
		l.mu.Unlock()
		var te *TxError
		if errors.As(err, &te) {
			log.Printf("rejected %s %s by %s: %s", p.TxID, p.Fn, p.Creator, te)
			writeJSON(w, te.Status, map[string]string{"code": te.Code, "error": te.Msg})
			return
		}
		writeJSON(w, 200, map[string]any{"credit": c, "block": blk})
	})
	mux.HandleFunc("GET /credits", func(w http.ResponseWriter, r *http.Request) {
		l.mu.Lock()
		out := make([]*credit.ConditionalCredit, 0, len(l.credits))
		for _, c := range l.credits {
			out = append(out, c)
		}
		l.mu.Unlock()
		sort.Slice(out, func(i, j int) bool {
			if out[i].CreatedAt != out[j].CreatedAt {
				return out[i].CreatedAt > out[j].CreatedAt
			}
			return out[i].TxnRef > out[j].TxnRef
		})
		writeJSON(w, 200, out)
	})
	mux.HandleFunc("GET /credits/{ref}", func(w http.ResponseWriter, r *http.Request) {
		l.mu.Lock()
		c, ok := l.credits[r.PathValue("ref")]
		l.mu.Unlock()
		if !ok {
			writeJSON(w, 404, map[string]string{"code": "NOT_FOUND", "error": "no such credit"})
			return
		}
		writeJSON(w, 200, c)
	})
	mux.HandleFunc("GET /chain", func(w http.ResponseWriter, r *http.Request) {
		l.mu.Lock()
		out := append([]Block{}, l.chain...)
		ok, h := l.verifyChain()
		l.mu.Unlock()
		writeJSON(w, 200, map[string]any{"blocks": out, "valid": ok, "height": h})
	})
	if allowClock {
		// Dev-only: move the ledger clock forward so a 4-hour hold can expire
		// during a live demo. Disabled unless DEVLEDGER_ALLOW_CLOCK=1.
		mux.HandleFunc("POST /dev/advance", func(w http.ResponseWriter, r *http.Request) {
			var body struct {
				Seconds int64 `json:"seconds"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Seconds <= 0 || body.Seconds > 7*24*3600 {
				writeJSON(w, 400, map[string]string{"code": "BAD_REQUEST", "error": "seconds must be in (0, 604800]"})
				return
			}
			l.mu.Lock()
			l.offset += body.Seconds
			now := l.now()
			l.mu.Unlock()
			writeJSON(w, 200, map[string]any{"now": now})
		})
	}
	return mux
}

// parseKeys reads "Org=base64pub,Org2=base64pub".
func parseKeys(s string) (map[string]ed25519.PublicKey, error) {
	keys := map[string]ed25519.PublicKey{}
	for _, part := range strings.Split(s, ",") {
		org, b64, ok := strings.Cut(strings.TrimSpace(part), "=")
		if !ok {
			continue
		}
		raw, err := base64.StdEncoding.DecodeString(b64)
		if err != nil || len(raw) != ed25519.PublicKeySize {
			return nil, fmt.Errorf("bad public key for %s", org)
		}
		keys[org] = raw
	}
	if len(keys) < 3 {
		return nil, errors.New("TWOKEY_PUBKEYS must list PayerBank, PayeeBank and NPCIObserver keys; run scripts/gen_keys.py")
	}
	return keys, nil
}

func main() {
	keys, err := parseKeys(os.Getenv("TWOKEY_PUBKEYS"))
	if err != nil {
		log.Fatal(err)
	}
	l := newLedger(keys)
	if path := os.Getenv("DEVLEDGER_DATA"); path != "" {
		if err := l.replay(path); err != nil {
			log.Fatal(err)
		}
		if l.logFile, err = os.OpenFile(path, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0o600); err != nil {
			log.Fatal(err)
		}
		log.Printf("replayed %d transactions from %s", len(l.chain), path)
	}
	addr := os.Getenv("DEVLEDGER_ADDR")
	if addr == "" {
		addr = "127.0.0.1:8081"
	}
	log.Printf("devledger listening on %s (orgs: %d)", addr, len(keys))
	srv := &http.Server{Addr: addr, Handler: l.routes(os.Getenv("DEVLEDGER_ALLOW_CLOCK") == "1"),
		ReadHeaderTimeout: 5 * time.Second}
	log.Fatal(srv.ListenAndServe())
}
