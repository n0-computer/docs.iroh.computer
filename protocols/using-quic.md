---
title: "Using QUIC"
---

## Why this matters for iroh

iroh is built on top of QUIC, providing peer-to-peer connectivity, NAT traversal, and encrypted connections out of the box. While iroh handles the hard parts of networking—holepunching, relay servers, and discovery—**you still need to design how your application exchanges data once connected**.

Many developers reach for iroh expecting it to completely abstract away the underlying transport. However, iroh intentionally exposes QUIC's powerful stream API because:

1. **QUIC is more expressive than TCP** - Multiple concurrent streams, fine-grained flow control, and cancellation give you tools TCP never had
2. **Protocol design matters** - How you structure requests, responses, and notifications affects performance, memory usage, and user experience
3. **No one-size-fits-all** - A file transfer protocol needs different patterns than a chat app or real-time collaboration tool

Think of iroh as giving you **reliable, secure tunnels between peers**. This guide shows you how to use QUIC's streaming patterns to build efficient protocols inside those tunnels. Whether you're adapting an existing protocol or designing something new, understanding these patterns will help you make the most of iroh's capabilities.

<Note>
iroh uses a fork of [Quinn](https://docs.rs/iroh-quinn/latest/iroh_quinn/), a pure-Rust implementation of QUIC maintained by [the n0 team](https://n0.computer). Quinn is production-ready, actively maintained, and used by projects beyond iroh. If you need lower-level QUIC access or want to understand the implementation details, check out the [Quinn documentation](https://docs.rs/iroh-quinn/latest/iroh_quinn/).
</Note>


## Overview of the QUIC API

Implementing a new protocol on the QUIC protocol can be a little daunting initially. Although the API is not that extensive, it's more complicated than e.g. TCP where you can only send and receive bytes, and eventually have an end of stream.
There isn't "one right way" to use the QUIC API. It depends on what interaction pattern your protocol intends to use.
This document is an attempt at categorizing the interaction patterns. Perhaps you find exactly what you want to do here. If not, perhaps the examples give you an idea for how you can utilize the QUIC API for your use case.
One thing to point out is that we're looking at interaction patterns *after* establishing a connection, i.e. everything that happens *after* we've `connect`ed or `accept`ed incoming connections, so everything that happens once we have a `Connection` instance.


Unlike TCP, in QUIC you can open multiple streams. Either side of a connection can decide to "open" a stream at any time:
```rs
impl Connection {
    async fn open_uni(&self) -> Result<SendStream, ConnectionError>;
    async fn accept_uni(&self) -> Result<Option<RecvStream>, ConnectionError>;
    
    async fn open_bi(&self) -> Result<(SendStream, RecvStream), ConnectionError>;
    async fn accept_bi(&self) -> Result<Option<(SendStream, RecvStream)>, ConnectionError>;
}
```
Similar to how each `write` on one side of a TCP-based protocol will correspond to a `read` on the other side, when a protocol `open`s a stream on one end, the other side of the protocol can `accept` such a stream.
Streams can be either uni-directional (`open_uni`/`accept_uni`), or bi-directional (`open_bi`/`accept_bi`).
- With uni-directional streams, only the opening side sends bytes to the accepting side. The receiving side can already start consuming bytes before the opening/sending side finishes writing all data. So it supports streaming, as the name suggests.
- With bi-directional streams, both sides can send bytes to each other at the same time. The API supports full duplex streaming.

<Note>
One bi-directional stream is essentially the closest equivalent to a TCP stream. If your goal is to adapt a TCP protocol to the QUIC API, the easiest way is going to be opening a single bi-directional stream and then essentially using the send and receive stream pair as if it were a TCP stream.
</Note>

Speaking of "finishing writing data", there are some additional ways to communicate information via streams besides sending and receiving bytes!
- The `SendStream` side can `.finish()` the stream. This will send something like an "end of stream notification" to the other side, *after all pending bytes have been sent on the stream.*
  This "notification" can be received on the other end in various ways:
	- `RecvStream::read` will return `Ok(None)`, if all pending data was read and the stream was finished. Other methods like `read_chunk` work similarly.
	- `RecvStream::read_to_end` will resolve once the finishing notification comes in, returning all pending data. **If the sending side never calls `.finish()`, this will never resolve**.
	- `RecvStream::stop` will resolve with `Ok(None)` if the stream was finished (or `Ok(Some(code))` if it was reset).
- The `SendStream` side can also `.reset()` the stream. This will have the same effect as `.finish()`ing the stream, except for two differences:
  Resetting will happen immediately and discard any pending bytes that haven't been sent yet. You can provide an application-specific "error code" (a `VarInt`) to signal the reason for the reset to the other side.
  This "notification" is received in these ways on the other end:
	- `RecvStream::read` and other methods like `read_exact`, `read_chunk` and `read_to_end` will return a `ReadError::Reset(code)` with the error code given on the send side.
	- `RecvStream::stop` will resolve to the error code `Ok(Some(code))`.
- The other way around, the `RecvStream` side can also notify the sending side that it's not interested in reading any more data by calling `RecvStream::stop` with an application-specific code.
  This notification is received on the *sending* side:
	- `SendStream::write` and similar methods like `write_all`, `write_chunk` etc. will error out with a `WriteError::Stopped(code)`.
	- `SendStream::stopped` resolves with `Ok(code)`.

<Note>
What is the difference between a bi-directional stream and two uni-directional streams?
1. The bi-directional stream establishes the stream pair in a single "open -> accept" interaction. For two uni-directional streams in two directions, you'd need one side to open, then send data, then accept at the same time. The other side would have to accept and then open a stream.
2. Two uni-directional streams can not be stopped or reset as a unit: One stream might be stopped or reset with one close code while the other is still open. Bi-directional streams can only be stopped or reset as a unit.
</Note>

These additional "notification" mechanisms are a common source of confusion: Naively, we might expect a networking API to be able to send and receive bytes, and maybe listen for a "stop".
However, it turns out that with the QUIC API, we can notify the other side about newly opened streams, and finish, reset, or even stop them. Additionally, there's two different types of stream-opening (uni-directional and bi-directional).
A bi-directional stream has 3 different ways *each side* can close *some* aspect of it: Each side can either `.finish()` or `.reset()` its send half, or `.stop()` its receiving half.

Finally, there's one more important "notification" we have to cover:
Closing the connection.

Either end of the connection can decide to close the connection at any point by calling `Connection::close` with an application-specific error code (a `VarInt`), (and even a bunch of bytes indicating a "reason", possibly some human-readable ASCII, but without a guarantee that it will be delivered).

Once this notification is received on the other end, all stream writes return `WriteError::ConnectionLost(ConnectionError::ApplicationClosed { .. })` and all reads return `ReadError::ConnectionLost(ConnectionError::ApplicationClosed { .. })`.
It can also be received by waiting for `Connection::closed` to resolve.

Importantly, this notification interrupts all flows of data:
- On the side that triggers it, it will drop all data to be sent
- On the side that receives it, it will immediately drop all data to be sent and the side will stop receiving new data.

What this means is that it's important to carefully close the connection at the right time, either at a point in the protocol where we know that we won't be sending or receiving any more data, or when we're sure we want to interrupt all data flows.

On the other hand, we want to make sure that we end protocols by sending this notification on at least one end of the connection, as just "leaving the connection hanging" on one endpoint causes the other endpoint to needlessly wait for more information, eventually timing out.

---

Let's look at some interaction pattern examples so we get a feeling for how all of these pieces fit together:

## Request and Response

The most common type of protocol interaction.
In this case, the connecting endpoint first sends a request.
The accepting endpoint will read the full request before starting to send a response.
Once the connecting endpoint has read the full response, it will close the connection.
The accepting endpoint waits for this close notification before shutting down.
```rs
async fn connecting_endpoint(conn: Connection, request: &[u8]) -> Result<Vec<u8>> {
    let (mut send, mut recv) = conn.open_bi().await?;
    send.write_all(request).await?;
    send.finish()?;
    
    let response = recv.read_to_end(MAX_RESPONSE_SIZE).await?;
    
    conn.close(0u32.into(), b"I have everything, thanks!");
    
    Ok(response)
}

async fn accepting_endpoint(conn: Connection) -> Result<()> {
    let (mut send, mut recv) = conn.accept_bi().await?.ok_or_else(|| anyhow!("connection closed"))?;
    let request = recv.read_to_end(MAX_REQUEST_SIZE).await?;
    
    let response = compute_response(&request);
    send.write_all(&response).await?;
    send.finish()?;
    
    conn.closed().await;
    
    Ok(())
}
```

## Full duplex Request & Response streaming

It's possible to start sending a response before the request has finished coming in.
This makes it possible to handle arbitrarily big requests in O(1) memory.
In this toy example we're reading `u64`s from the client and send back each of them doubled.

```rs
async fn connecting_endpoint(conn: Connection, mut request: impl Stream<Item = u64>) -> Result<()> {
    let (mut send, mut recv) = conn.open_bi().await?;
    
    // concurrently read the responses
    let read_task = tokio::spawn(async move {
	    let mut buf = [0u8; size_of::<u64>()];
	    // read_exact will return `Err` once the other side
	    // finishes its stream
        while recv.read_exact(&mut buf).await.is_ok() {
	        let number = u64::from_be_bytes(buf);
            println!("Read response: {number}");
        }
    });
    
    while let Some(number) = request.next().await {
        let bytes = number.to_be_bytes();
        send.write_all(&bytes).await?;
    }
    send.finish()?;
    
    // we close the connection after having read all data
    read_task.await?;
    conn.close(0u32.into(), b"done");
    
    Ok(())
}

async fn accepting_endpoint(conn: Connection) -> Result<()> {
    let (mut send, mut recv) = conn.accept_bi().await?.ok_or_else(|| anyhow!("connection closed"))?;
    
    let mut buf = [0u8; size_of::<u64>()];
    while recv.read_exact(&mut buf).await.is_ok() {
	    let number = u64::from_be_bytes(buf);
	    let doubled = number.wrapping_mul(2).to_be_bytes();
	    send.write_all(&doubled).await?;
    }
    send.finish()?;
    
	// the other side will tell us when it's done reading our data
	conn.closed().await;
	
	Ok(())
}
```

## Multiple Requests & Responses

This is one of the main use cases QUIC was designed for: Multiplex multiple requests and responses on the same connection.
HTTP3 is an example for a protocol using QUIC's capabilities for this.
A single HTTP3 connection to a server can handle multiple HTTP requests concurrently without the requests blocking each other.
This is the main innovation in HTTP3: It makes HTTP/2's connection pool obsolete.

In HTTP3, each HTTP request is run as its own bi-directional stream. The request is sent in one direction while the response is received in the other direction. This way both stream directions are cancellable as a unit, this makes it possible for the user agent to cancel some HTTP requests without cancelling any others in the same HTTP3 connection.

Using the QUIC API for this purpose will feel very natural:
```rs
// The connecting endpoint can call this multiple times
// for one connection.
// When it doesn't want to do more requests and has all
// responses, it can close the connection.
async fn request(conn: &Connection, request: &[u8]) -> Result<Vec<u8>> {
    let (mut send, mut recv) = conn.open_bi().await?;
    send.write_all(request).await?;
    send.finish()?;
    
    let response = recv.read_to_end(MAX_RESPONSE_SIZE).await?;
    
    Ok(response)
}

// The accepting endpoint will call this to handle all
// incoming requests on a single connection.
async fn handle_requests(conn: Connection) -> Result<()> {
    loop {
        let stream = conn.accept_bi().await?;
        match stream {
            Some((send, recv)) => {
                tokio::spawn(handle_request(send, recv));
            }
            None => break, // connection closed
        }
    }
    Ok(())
}

async fn handle_request(mut send: SendStream, mut recv: RecvStream) -> Result<()> {
    let request = recv.read_to_end(MAX_REQUEST_SIZE).await?;
    
    let response = compute_response(&request);
    send.write_all(&response).await?;
    send.finish()?;
    
    Ok(())
}
```

Please note that, in this case, the client doesn't immediately close the connection after a single request (duh!). Instead, it might want to optimistically keep the connection open for some idle time or until it knows the application won't need to make another request, and only then close the connection. All that said, it's still true that **the connecting side closes the connection**.

## Multiple ordered Notifications

Sending and receiving multiple notifications that can be handled one-by-one can be done by adding framing to the bytes on a uni-directional stream.

```rs
async fn connecting_endpoint(conn: Connection, mut notifications: impl Stream<Item = Bytes> + Unpin) -> Result<()> {
    let mut send = conn.open_uni().await?;
    
    let mut send_frame = LengthDelimitedCodec::builder().new_write(send);
    while let Some(notification) = notifications.next().await {
        send_frame.send(notification).await?;
    }
    
    send_frame.get_mut().finish()?;
    conn.closed().await;
    
    Ok(())
}

async fn accepting_endpoint(conn: Connection) -> Result<()> {
    let recv = conn.accept_uni().await?.ok_or_else(|| anyhow!("connection closed"))?;
    let mut recv_frame = LengthDelimitedCodec::builder().new_read(recv);
    
    while let Some(notification) = recv_frame.try_next().await? {
        println!("Received notification: {notification:?}");
    }
    
    conn.close(0u32.into(), b"got everything!");
    
    Ok(())
}
```

Here we're using `LengthDelimitedCodec` and `tokio-util`'s `codec` feature to easily turn the `SendStream` and `RecvStream` that work as streams of bytes into streams of items, where each item in this case is a `Bytes`/`BytesMut`. In practice you would probably add byte parsing to this code first, and you might want to configure the `LengthDelimitedCodec`.

The resulting notifications are all in order since the bytes in the uni-directional streams are received in-order, and we're processing one frame before continuing to read the next bytes off of the QUIC stream.

<Note>
There's another somewhat common way of doing this:
The order that `accept_uni` come in will match the order that `open_uni` are called on the remote endpoint. (The same also goes for bi-directional streams.)
This way you would receive one notification per stream and know the order of notifications from the stream ID/the order of accepted streams.
The downside of doing it that way is you will occupy more than one stream. If you want to multiplex other things on the same connection, you'll need to add some signaling.
</Note>


## Request with multiple Responses

If your protocol expects multiple responses for a request, we can implement that with the same primitive we've learned about in the section about multiple ordered notifications: We use framing to segment a single response byte stream into multiple ordered responses:

```rs
async fn connecting_endpoint(conn: Connection, request: &[u8]) -> Result<()> {
    let (mut send, recv) = conn.open_bi().await?;
	send.write_all(request).await?;
	send.finish()?;
	
	let mut recv_frame = LengthDelimitedCodec::builder().new_read(recv);
	while let Some(response) = recv_frame.try_next().await? {
	    println!("Received response: {response:?}");
	}
	
	conn.close(0u32.into(), b"thank you!");
    
    Ok(())
}

async fn accepting_endpoint(conn: Connection) -> Result<()> {
	let (send, mut recv) = conn.accept_bi().await?.ok_or_else(|| anyhow!("connection closed"))?;
	let request = recv.read_to_end(MAX_REQUEST_SIZE).await?;
	
	let mut send_frame = LengthDelimitedCodec::builder().new_write(send);
	let mut responses = responses_for_request(&request);
	while let Some(response) = responses.next().await {
	    send_frame.send(response).await?;
	}
	send_frame.get_mut().finish()?;

	conn.closed().await;
	
	Ok(())
}

fn responses_for_request(req: &[u8]) -> impl Stream<Item = Bytes> {
    // ...
}
```

This example ends up similar as the one with ordered notifications, except
1. The roles are reversed: The length-prefix sending happens on the accepting endpoint, and the length-prefix decoding on the connecting endpoint.
2. We additionally send a request before we start receiving multiple responses.

<Note>
At this point you should have a good feel for how to write request/response protocols using the QUIC API. For example, you should be able to piece together a full-duplex request/response protocol where you're sending the request as multiple frames and the response comes in with multiple frames, too, by combining two length delimited codes in both ways and taking notes from the full duplex section further above.
</Note>

## Requests with multiple unordered Responses

The previous example required all responses to come in ordered.
What if that's undesired? What if we want the connecting endpoint to receive incoming responses as quickly as possible?
In that case, we need to break up the single response stream into multiple response streams.
We can do this by "conceptually" splitting the "single" bi-directional stream into one uni-directional stream for the request and multiple uni-directional streams in the other direction for all the responses:

```rs
async fn connecting_side(conn: Connection, request: &[u8]) -> Result<()> {
    let mut send = conn.open_uni().await?;
    send.write_all(request).await?;
    send.finish()?;
    
    let recv_tasks = TaskTracker::new();
    // accept_uni will return `Ok(None)` once the connection is closed
    loop {
        match conn.accept_uni().await? {
            Some(recv) => {
                recv_tasks.spawn(handle_response(recv));
            }
            None => break,
        }
    }
	recv_tasks.wait().await;
	conn.close(0u32.into(), b"Thank you!");
	
	Ok(())
}
```

<Note>
You might've noticed that this destroys the "association" between the two stream directions. This means we can't use tricks similar to what HTTP3 does that we described above to multiplex multiple request-responses interactions on the same connection.
This is unfortunate, but can be fixed by prefixing your requests and responses with a unique ID chosen per request. This ID then helps associate the responses to the requests that used the same ID.
Another thing that might or might not be important for your use case is knowing when unordered stream of responses is "done":
You can either introduce another message type that is interpreted as a finishing token, but there's another elegant way of solving this. Instead of only opening a uni-directional stream for the request, you open a bi-directional one. The response stream will only be used to indicate the final response stream ID. It then acts as a sort of "control stream" to provide auxiliary information about the request for the connecting endpoint.
</Note>

## Time-sensitive Real-time interaction

We often see users reaching for the QUIC datagram extension when implementing real-time protocols. Doing this is in most cases misguided.
QUIC datagram sending still interacts with QUIC's congestion controller and thus are also acknowledged.
Implementing traditional protocols on top of QUIC datagrams might thus not perform the way they were designed to.
Instead, it's often better to use lots of streams that are then stopped, reset or prioritized.

A real-world example is the media over QUIC protocol (MoQ in short): MoQ is used to transfer live video frames. It uses one QUIC stream for each frame (QUIC streams are cheap to create)!

The receiver then stops streams that are "too old" to be delivered, e.g. because it's a live video stream and newer frames were already fully received.
Similarly, the sending side will also reset older streams for the application level to indicate to the QUIC stack it doesn't need to keep re-trying the transmission of an outdated live video frame. (MoQ will actually also use stream prioritization to make sure the newest video frames get scheduled to be sent first.)

## Closing Connections

Gracefully closing connections can be tricky to get right when first working with the QUIC API.
If you don't close connections gracefully, you'll see the connecting timing out on one endpoint, usually after 30s, even though another endpoint finishes promptly without errors.
This happens when the endpoint that finishes doesn't notify the other endpoint about having finished operations.
There's mainly two reasons this happens:
1. The protocol doesn't call `Connection::close` at the right moment.
2. The endpoint that closes the connection is immediately dropped afterwards without waiting for `Endpoint::close`.
To make sure that you're not hitting (2), simply always make sure to wait for `Endpoint::close` to resolve, on both `Endpoint`s, if you can afford it.
Getting (1) right is harder. We might accidentally close connections too early, because we accidentally drop the `Connection` (which implicitly calls close). Instead, we should always keep around the connection and either wait for `Connection::closed` to resolve or call `Connection::close` ourselves at the right moment. When that is depends on what kind of protocol you're implementing:
### After a single Interaction

Protocols that implement a single interaction want to keep their connection alive for only the time of this interaction.
In this case, the endpoint that received application data last will be the endpoint that calls `Connection::close` at that point in time.
Conversely, the other endpoint should wait for `Connection::closed` to resolve before ending its operations.
An example of this can be seen in the [Request and Response](#request-and-response) section above: The connecting side closes the connection once it received the response and the accepting side waits for the connection to be closed after having sent off the response.

### During continuous Interaction

Sometimes we want to keep open connections as long as the user is actively working with the application, so we don't needlessly run handshakes or try to hole-punch repeatedly.
In these cases, the protocol flow doesn't indicate which endpoint of the connection will be the one that closes the connection.
Instead, clients should concurrently monitor `Connection::closed` while they're running the protocol:

```rs
async fn handle_connection(conn: Connection) -> Result<()> {
    futures_lite::future::race(
		run_protocol(conn.clone()),
		async move {
			conn.closed().await;
			anyhow::Ok(())
		},
    ).await?;
    Ok(())
}

async fn run_protocol(conn: Connection) -> Result<()> {
	// run normal protocol flow
	// once we realize we want to abort the connection flow
	conn.close(0u32.into(), b"ah sorry, have to go!");
	
	Ok(())
}
```

And again, after `handle_connection` we need to make sure to wait for `Endpoint::close` to resolve.

## Aborting Streams

Sometimes you need to abandon a stream before it completes - either because the data has become stale, or because you've decided you no longer need it. QUIC provides mechanisms for both the sender and receiver to abort streams gracefully.

### When to abort streams

A real-world example comes from Media over QUIC (MoQ), which streams live video frames. Consider this scenario:

- Each video frame is sent on its own uni-directional stream
- Frames arrive out of order due to network conditions
- By the time an old frame finishes transmitting, newer frames have already been received
- Continuing to receive the old frame wastes bandwidth and processing time

### How to abort streams

**From the sender side:**

Use `SendStream::reset(error_code)` to immediately stop sending data and discard any buffered bytes. This tells QUIC to stop retrying lost packets for this stream.


**From the receiver side:**

Use `RecvStream::stop(error_code)` to tell the sender you're no longer interested in the data. This allows the sender's QUIC stack to stop retransmitting lost packets.


### Key insights

1. **Stream IDs indicate order**: QUIC stream IDs are monotonically increasing. You can compare stream IDs to determine which streams are newer without relying on application-level sequencing.

2. **Both sides can abort**: Either the sender (via `reset`) or receiver (via `stop`) can abort a stream. Whichever side detects the data is no longer needed first should initiate the abort.

3. **QUIC stops retransmissions**: When a stream is reset or stopped, QUIC immediately stops trying to recover lost packets for that stream, saving bandwidth and processing time.

4. **Streams are cheap**: Opening a new stream is very fast (no round-trips required), so it's perfectly fine to open one stream per video frame, message, or other small unit of data.

This pattern of using many short-lived streams that can be individually aborted is one of QUIC's most powerful features for real-time applications. It gives you fine-grained control over what data is worth transmitting, without the head-of-line blocking issues that would occur with a single TCP connection.

## QUIC 0-RTT features

### Server-side 0.5-RTT

QUIC connections always take 1 full round-trip time (RTT) to establish - the client sends a hello, the server responds with its hello and certificate, and only then can application data flow. However, the server can actually start sending application data **before** the client finishes the handshake, achieving what's called "0.5-RTT" latency.

This works because after the server sends its hello, it doesn't need to wait for the client's final handshake message before it can start processing the client's request and sending a response. The server knows the encryption keys at this point and can immediately begin sending data back.

**How to use it:**

On the server side, this happens automatically - you don't need to do anything special. As soon as you `accept_bi()` or `accept_uni()` a stream, you can start writing to it immediately, even if the handshake hasn't fully completed on the client side yet.

```rs
async fn accepting_endpoint(conn: Connection) -> Result<()> {
    let (mut send, mut recv) = conn.accept_bi().await?.ok_or_else(|| anyhow!("connection closed"))?;
    let request = recv.read_to_end(MAX_REQUEST_SIZE).await?;
    
    // We can start sending the response immediately without waiting
    // for the client to finish the handshake
    let response = compute_response(&request);
    send.write_all(&response).await?;
    send.finish()?;
    
    conn.closed().await;
    Ok(())
}
```

**Important gotcha: Request replay attacks**

Because the server starts processing before the handshake completes, there's a security consideration: the client's initial request data could potentially be replayed by an attacker who intercepts the handshake packets. This means:

- **The server should treat 0.5-RTT requests as potentially non-idempotent**
- Avoid performing actions with side effects (like making payments, deleting data, etc.) based solely on 0.5-RTT data
- If your protocol requires idempotency guarantees, wait for the handshake to complete before processing sensitive operations

For read-only operations or idempotent requests, 0.5-RTT is perfectly safe and provides a nice latency improvement.
