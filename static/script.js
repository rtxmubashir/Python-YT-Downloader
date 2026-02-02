function download(){
    const url=document.getElementById("url").value;
    const format=document.getElementById("format").value;
    const status=document.getElementById("status");

    if(!url){
        status.innerText="Please enter a URL";
        return;
    }

    status.innerText="Downloading... please wait";

    fetch("/download",{
        method:"POST",
        headers:{
            "Content-Type":"application/json"
        },
        body:JSON.stringify({url,format})
    })
    .then(res=>{
        if(!res.ok)throw new Error("Download failed");
        return res.blob();
    })
    .then(blob=>{
        const a=document.createElement("a");
        a.href=URL.createObjectURL(blob);
        a.download="download";
        a.click();
        status.innerText="Download complete";
    })
    .catch(err=>{
        status.innerText="Error downloading video";
    });
}
