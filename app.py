from flask import Flask,render_template,request,send_file,jsonify
import yt_dlp,os,uuid

app=Flask(__name__)
DOWNLOAD_DIR="downloads"
os.makedirs(DOWNLOAD_DIR,exist_ok=True)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/download",methods=["POST"])
def download():
    data=request.get_json()
    url=data.get("url")
    format_type=data.get("format")

    if not url:
        return jsonify({"error":"No URL provided"}),400

    uid=str(uuid.uuid4())

    if format_type=="mp3":
        ydl_opts={
            "format":"bestaudio",
            "outtmpl":f"{DOWNLOAD_DIR}/{uid}.%(ext)s",
            "postprocessors":[{
                "key":"FFmpegExtractAudio",
                "preferredcodec":"mp3",
                "preferredquality":"192",
            }],
        }
    else:
        ydl_opts={
            "format":"best",
            "outtmpl":f"{DOWNLOAD_DIR}/{uid}.%(ext)s",
        }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info=ydl.extract_info(url)
            filepath=ydl.prepare_filename(info)

        return send_file(filepath,as_attachment=True)

    except Exception as e:
        return jsonify({"error":str(e)}),500

if __name__=="__main__":
    app.run(debug=True)
