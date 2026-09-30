import assert from "node:assert/strict";
import { test } from "node:test";
import { siteOf, siteSaidIn } from "./sites.js";

test("naming a well-known site vouches for its real address and that site's own subdomains", () => {
  const words = "Search Andrew Tate on YouTube";
  assert.ok(siteSaidIn("youtube.com", words));
  assert.ok(siteSaidIn("m.youtube.com", words));
  assert.ok(siteSaidIn("www.youtube.com", words));
  assert.ok(siteSaidIn("youtu.be", words));
  assert.ok(siteSaidIn("stackoverflow.com", "look it up on stack overflow"));
  assert.ok(siteSaidIn("news.bbc.co.uk", "what's on the BBC"));
});

test("a name vouches for no other address, even one that contains it", () => {
  const words = "Search Andrew Tate on YouTube";
  for (const host of ["youtube.lol", "andrew.lol", "attacker.andrew.io", "youtube.com.evil.io", "tate.com", "you.com"]) {
    assert.ok(!siteSaidIn(host, words), host);
  }
  // A name that is not a well-known site vouches for nothing.
  assert.ok(!siteSaidIn("andrew.com", "open Andrew"));
});

test("an address the person spelled out is theirs", () => {
  assert.ok(siteSaidIn("youtube.lol", "open youtube dot lol"));
  assert.ok(siteSaidIn("youtube.lol", "Open youtube.lol please"), "as a transcript writes it");
  assert.ok(siteSaidIn("my-site.com", "go to my site dot com"));
  assert.ok(siteSaidIn("attacker.andrew.io", "open andrew dot io"), "a subdomain of the address they said");
  assert.ok(siteSaidIn("192.168.1.1", "open 192.168.1.1"));
});

test("without the dot, the words are just words", () => {
  assert.ok(!siteSaidIn("youtube.lol", "open youtube lol"));
  assert.ok(!siteSaidIn("youtube.lol", "Open YouTube. Lol, that was quick"), "a full stop between sentences is not a dot");
  assert.ok(!siteSaidIn("192.168.1.1", "open 192"));
});

test("on shared hosting, each subdomain is someone else's site", () => {
  assert.deepEqual(siteOf("attacker.github.io"), ["attacker", "github", "io"]);
  assert.ok(!siteSaidIn("attacker.github.io", "open GitHub"));
  assert.ok(!siteSaidIn("attacker.github.io", "open github dot io"));
  assert.ok(siteSaidIn("attacker.github.io", "open attacker dot github dot io"));
  assert.deepEqual(siteOf("shop.example.co.uk"), ["example", "co", "uk"]);
  assert.deepEqual(siteOf("www.youtube.com."), ["youtube", "com"]);
});
