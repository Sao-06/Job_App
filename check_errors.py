import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page()
        
        errors = []
        page.on("console", lambda msg: errors.append(f"CONSOLE {msg.type}: {msg.text}") if msg.type in ['error', 'warning'] else None)
        page.on("pageerror", lambda err: errors.append(f"PAGE ERROR: {err}"))
        
        await page.goto("http://localhost:8000/app")
        await asyncio.sleep(2)
        
        for e in errors:
            print(e)
            
        await browser.close()

asyncio.run(main())
