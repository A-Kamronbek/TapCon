# TapCon server access

How to get onto the production VPS, how to let someone else on, and how to
take access away again.

**The server:** the address, the login user and the recovery route are in
`deploy/SERVER-ACCESS.local.md`, which is deliberately not in this
repository — ask whoever runs the deployment for it. Everything below is
the procedure; that file fills in the blanks.

**The rule:** this server accepts **SSH keys only**. Passwords are switched
off at the server, so no password will ever work, however correct it is. In
its first day online it logged 126 failed password attempts from the
internet; with passwords off, none of them can ever succeed. The server holds
`FIELD_ENCRYPTION_KEY`, which decrypts every seller's payment credentials —
that is what is actually being protected here.

---

## 1. Logging in (once you have access)

```bash
ssh tapcon
```

That works because of an entry in `~/.ssh/config` (see §2, step 4). On
Windows use **Git Bash**, not PowerShell. Windows' built-in OpenSSH client
can be installed but broken — if `ssh -V` exits non-zero and prints nothing,
that is the symptom; Git's own copy at
`C:\Program Files\Git\usr\bin\ssh.exe` works instead.

Without the alias, the long form is:

```bash
ssh <user>@<SERVER_IP>
```

---

## 2. Adding another of *your own* devices

Do this on the new device (laptop, second PC, phone).

### Step 1 — make a key on the new device

```bash
ssh-keygen -t ed25519 -C "kamron-laptop"
```

Press Enter to accept the default path. **Set a passphrase** if the device
travels — it is what protects the key if the laptop is stolen.

This creates two files:

| File | What it is | Who may see it |
|---|---|---|
| `~/.ssh/id_ed25519` | the **private** key | nobody, ever |
| `~/.ssh/id_ed25519.pub` | the **public** key | anyone; this is the one you send |

Never send, paste, screenshot or commit the file **without** `.pub`. If you
ever do, treat the key as burnt and make a new one.

### Step 2 — copy the public key

```bash
cat ~/.ssh/id_ed25519.pub
```

One line, starting `ssh-ed25519 AAAA…`. Copy the whole line.

### Step 3 — install it, from a device that already has access

```bash
ssh tapcon
echo 'PASTE_THE_PUBLIC_KEY_LINE_HERE' >> ~/.ssh/authorized_keys
grep -c . ~/.ssh/authorized_keys      # should go up by exactly 1
exit
```

If you have no working device left, see §5.

### Step 4 — make the short alias on the new device

Create or append to `~/.ssh/config`:

```
Host tapcon
    HostName <SERVER_IP>
    User ubuntu
    IdentityFile ~/.ssh/id_ed25519
    ServerAliveInterval 30
```

Test it: `ssh tapcon` should log straight in.

### Phones

Use **Termius** (iOS/Android) or **Blink** (iOS). Both can generate a key in
the app; export its *public* half and install it with step 3. Do not copy a
private key onto a phone from a computer.

---

## 3. Adding **another person** (a teammate)

Do **not** give them the `ubuntu` key. Two people sharing one key means you
cannot tell who did what, and you cannot remove one person's access without
locking out the other. Give them their own account.

Ask them for their **public** key line (step 2 above), then, signed in as the deploy user:

```bash
NEWUSER=azizbek          # their name, lowercase, no spaces
PUBKEY='ssh-ed25519 AAAA… their-key-here'

sudo adduser --disabled-password --gecos "" "$NEWUSER"
sudo install -d -m 700 -o "$NEWUSER" -g "$NEWUSER" "/home/$NEWUSER/.ssh"
echo "$PUBKEY" | sudo tee "/home/$NEWUSER/.ssh/authorized_keys" >/dev/null
sudo chown "$NEWUSER:$NEWUSER" "/home/$NEWUSER/.ssh/authorized_keys"
sudo chmod 600 "/home/$NEWUSER/.ssh/authorized_keys"
```

`--disabled-password` is deliberate: the account can be used with a key, but
has no password anyone could guess or phish.

Then decide what they may do — pick **one**:

```bash
# (a) Full admin, same as you. For someone you trust with the payment keys.
sudo usermod -aG sudo "$NEWUSER"

# (b) Deploy only. They can ship code and restart the app, and nothing else.
sudo usermod -aG tapcon "$NEWUSER"
```

Prefer (b) until you have a reason for (a). Someone in the `sudo` group can
read `.env`, and `.env` contains the key that decrypts every seller's
provider credentials.

They log in with **their own name**:

```bash
ssh <their-name>@<SERVER_IP>
```

Check it worked before they need it:

```bash
sudo ls -l /home/$NEWUSER/.ssh/authorized_keys   # -rw------- 1 azizbek azizbek
groups $NEWUSER
```

---

## 4. Removing access

**A device you lost** — delete just that key's line. Every key line ends in
its comment (`kamron-laptop`), which is what makes them tellable apart:

```bash
ssh tapcon
cp ~/.ssh/authorized_keys ~/.ssh/authorized_keys.bak
grep -v 'kamron-laptop' ~/.ssh/authorized_keys.bak > ~/.ssh/authorized_keys
grep -c . ~/.ssh/authorized_keys        # one fewer than before
```

Keep another terminal open and test a fresh `ssh tapcon` **before** closing
the one you are in. If you removed the wrong line:
`cp ~/.ssh/authorized_keys.bak ~/.ssh/authorized_keys`.

**A person who left:**

```bash
sudo pkill -u azizbek || true          # end their live sessions
sudo usermod -L azizbek                # lock the account
sudo usermod -aG '' azizbek 2>/dev/null || sudo deluser azizbek sudo
```

Lock first, delete later — `sudo deluser --remove-home azizbek` once you are
sure nothing of theirs is needed. And **if they ever had `sudo`, rotate the
secrets**: they could have read `.env`. See `deploy/RESTORE.md` for what
rotating `FIELD_ENCRYPTION_KEY` involves — it is not a small job, which is
the real argument for giving out option (b) rather than (a).

---

## 5. If you are locked out of everything

Passwords are off, so a lost key cannot be worked around from the internet.
The way back in is OVH's out-of-band console:

1. Sign in to the OVH manager.
2. Open the VPS → the `...` menu → **KVM / Console** (some plans call it
   "Web console" or offer VNC).
3. That gives you a screen as if you were sitting at the machine. Log in as
   `ubuntu` there — or use **Reset root password** in the same menu first if
   you do not know it.
4. Add your new public key to `~/.ssh/authorized_keys` as in §2 step 3.

**Do this once now, while you do not need it.** Confirming the console opens
takes a minute today, and is the difference between an inconvenience and an
emergency later.

---

## 6. Things that will bite you

**A hardening file named `99-…` does nothing.** `sshd` keeps the **first**
value it reads for each keyword, and `/etc/ssh/sshd_config` includes
`/etc/ssh/sshd_config.d/*.conf` in alphabetical order. This server ships
`50-cloud-init.conf` containing `PasswordAuthentication yes`, so a `99-` file
saying `no` loses silently while looking completely correct. Ours is
`00-tapcon-hardening.conf`. If you ever change SSH settings, put them there
and **verify the result rather than the file**:

```bash
sudo sshd -T | grep -E 'passwordauthentication|permitrootlogin|pubkeyauthentication'
```

**Always validate before reloading.** A broken config plus a reload is how
people lock themselves out for real:

```bash
sudo sshd -t && sudo systemctl reload ssh
```

**Never `chmod 777` anything under `~/.ssh`.** SSH refuses to use keys with
loose permissions and the error does not say so clearly. Correct is `700` on
the directory, `600` on `authorized_keys` and on private keys.

**fail2ban is watching.** Four failed attempts in ten minutes bans the IP for
an hour. If you suddenly cannot connect from your own network, that is the
first thing to check — from another device:

```bash
ssh tapcon 'sudo fail2ban-client status sshd'
sudo fail2ban-client set sshd unbanip YOUR.IP.HERE
```

---

## 7. What is currently configured

| Setting | Value | Why |
|---|---|---|
| Password login | **off** | constant automated guessing from the internet |
| Root SSH login | **off** | the login user has `sudo`; root is not needed |
| SSH port | 22 | changing it stops noise, not attackers |
| Firewall (ufw) | 22, 80, 443 in; all else denied | PostgreSQL is not on it deliberately |
| fail2ban | 4 tries / 10 min → 1 h ban | |
| Automatic updates | security only | |
| PostgreSQL | `listen_addresses = localhost` | never reachable from the internet |
| Timezone | Asia/Tashkent | so cron and logs match a seller's day |
