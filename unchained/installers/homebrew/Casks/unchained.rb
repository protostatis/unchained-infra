cask "unchained" do
  version "0.5.2"
  sha256 "47694b908272537a3755a13b3a79b0c8344d9e2cebc1bfadaebd697dbd0e85cb"

  # Public download; the account gate is in the app's Connect step, not here.
  url "https://unchainedsky.com/web/download-desktop",
      verified: "unchainedsky.com"
  name "Unchained"
  desc "Chat and its own Chromium, with the agent driving that browser"
  homepage "https://unchainedsky.com/desktop"

  depends_on arch: :arm64
  depends_on macos: ">= :ventura"

  app "Unchained.app"

  zap trash: [
    "~/Library/Application Support/Unchained",
    "~/Library/Caches/com.unchainedsky.desktop",
    "~/Library/Preferences/com.unchainedsky.desktop.plist",
  ]
end
