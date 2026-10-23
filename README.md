# Comix-Uploader

Command-line chapter uploader for [comix.to](https://comix.to).
comix.to has no public upload API, so this uses the same private API the site's
own "Upload chapter" page uses (see [API-NOTES.md](API-NOTES.md)).

Features:
- Upload one archive (`--zip`) or a whole folder of chapter folders/archives (`--folder`)
- Skips chapters you've already uploaded (or replaces the file with `--reupload`)
- Chunked upload with retries; the server's own byte count is used to resume
- Logs in with the comix.to cookies from your Firefox profile, no password needed
- `--list-uploads` shows each upload's review status; `--delete` removes one
- Prints `FINAL_URL: ...` per chapter for release scripts

New uploads go into comix.to's review queue (`pending`) before they're public.

---

## Setup

1. Install the dependency:
   ```
   py -m pip install -r requirements.txt
   ```
2. Log in to comix.to in **Firefox** once and tick "remember me". The tool reads the
   login cookie from Firefox's profile. (Chrome 130+ encrypts its cookies so they
   can only be read as admin, which is why Firefox is used.)
   Alternatively, export a `cookies.txt` for comix.to and set `cookie_source = file`.
3. Copy `config.ini.example` to `config.ini` and fill in:
   - `username` / `user_id`: your comix.to account (the id is the `abc123` in `comix.to/u/abc123`).
     Uploads are refused if Firefox is logged in to a different account.
   - `group_id` / `group_name`: the group to credit (the number in `comix.to/groups/<id>`).
4. Check it works:
   ```
   py comix-upload.py --whoami
   ```

## series.json

Maps series names to comix.to title hash ids (the `abc12` in
`comix.to/title/abc12-some-name`). Copy `series.json.example` to `series.json`.

```json
{
  "My Favorite Series": { "hid": "abc12", "folder": "K:\\path\\to\\chapters" },
  "Another Series": "xyz9"
}
```

`py comix-upload.py --group-titles` lists every title your group has, with hash ids,
to fill this in. A series your group hasn't uploaded to yet won't be listed; take its
hash id from its comix.to URL.

## Usage

```
py comix-upload.py --series "My Favorite Series" --zip "K:\path\ch012.zip" --chapter 12 --volume 2 --title "Title"
py comix-upload.py --manga abc12 --zip "K:\path\V02 Ch012 Title.zip"
py comix-upload.py --series "My Favorite Series" --folder "K:\path\chapters" --start 1 --end 20 --exclude 7
py comix-upload.py --series "My Favorite Series" --zip "K:\path\ch012.zip" --chapter 12 --reupload
py comix-upload.py --dry-run --series "My Favorite Series" --folder "K:\path\chapters"
py comix-upload.py --list-uploads
py comix-upload.py --delete 11500000
```

- `--manga` takes a hash id or a full title URL.
- With `--zip` and no `--chapter`, the chapter/volume/title are read from the file name.
- `--folder` reads names like `V01 Ch001 Title`, `Vol.1 Ch.5 Title`, `Ch003.5 Half`
  (folders of images are zipped on the fly; archives are sent as-is).
- `--group 0` uploads with no group credited; `--official` marks an official release.
- Exit code is 1 if any chapter failed.

## Limits

- Archives: .zip / .cbz / .cbr / .rar / .7z, up to 1 GB, pages in reading order.
- The "already uploaded" check only knows about **your** uploads. Chapters that were
  on comix.to before (imported releases) can't be listed without the site's
  browser-generated request token, so the tool only prints a note when a chapter
  number is at or below the title's latest chapter.
